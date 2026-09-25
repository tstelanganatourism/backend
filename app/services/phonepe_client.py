import base64
import hashlib
import hmac
import time
import httpx
from loguru import logger
from fastapi import HTTPException, status
from typing import Optional
from app.core.config import settings


class PhonePeService:
    def __init__(self):
        self.merchant_id = settings.PHONEPE_MERCHANT_ID
        self.client_id = settings.PHONEPE_CLIENT_ID
        self.client_secret = settings.PHONEPE_CLIENT_SECRET
        self.client_version = str(settings.PHONEPE_CLIENT_VERSION or "1")
        self.salt_key = settings.PHONEPE_SALT_KEY  # v1-style salt key used for webhook verification
        self.salt_index = str(settings.PHONEPE_SALT_INDEX or "1")
        self.env = settings.PHONEPE_ENV or "PRODUCTION"

        if self.env.upper() in ("PROD", "PRODUCTION"):
            self.base_url = "https://api.phonepe.com/apis/pg"
            self.oauth_url = "https://api.phonepe.com/apis/identity-manager/v1/oauth/token"
        else:
            self.base_url = "https://api-preprod.phonepe.com/apis/pg-sandbox"
            self.oauth_url = "https://api-preprod.phonepe.com/apis/pg-sandbox/v1/oauth/token"

        # Service is in MOCK mode only when no credentials at all are configured
        if self.env.upper() == "MOCK" or (not self.client_id and not self.client_secret):
            logger.warning("PhonePe Service initialized in MOCK mode. Mocking payment gateway.")
            self.is_mock = True
        else:
            self.is_mock = False
            logger.info(
                f"PhonePe Service initialized in {self.env} mode. "
                f"MerchantID={self.merchant_id}, ClientID={self.client_id}, Base={self.base_url}"
            )

        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # OAuth Token Management
    # ─────────────────────────────────────────────────────────────────────────

    async def _get_oauth_token(self) -> str:
        """Fetch and cache a PhonePe V2 OAuth access token."""
        if self._cached_token and time.time() < self._token_expires_at - 60:
            return self._cached_token

        token_payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "client_version": self.client_version,
            "grant_type": "client_credentials",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    self.oauth_url,
                    data=token_payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            if response.status_code == 200:
                token_data = response.json()
                token = token_data.get("access_token")
                expires_in = token_data.get("expires_in", 900)
                if token:
                    self._cached_token = token
                    self._token_expires_at = time.time() + float(expires_in)
                    logger.info(f"PhonePe OAuth token cached. Expires in {expires_in}s.")
                    return token

            logger.error(
                f"PhonePe OAuth token fetch failed: {response.status_code} — {response.text[:300]}"
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to authenticate with payment provider.",
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"PhonePe OAuth connection error: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Connection to payment authentication server failed.",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Payment Initiation
    # ─────────────────────────────────────────────────────────────────────────

    async def create_payment_url(
        self,
        amount: float,
        transaction_id: str,
        user_id: str,
        redirect_url: str,
        callback_url: str,
        phone_number: Optional[str] = None,
    ) -> dict:
        """Create a PhonePe V2 checkout session and return the redirect URL."""
        amount_paise = int(round(amount * 100))

        if self.is_mock:
            mock_url = (
                f"{settings.FRONTEND_URL}/payment-status"
                f"?merchantTransactionId={transaction_id}&merchantId=MOCK&code=PAYMENT_SUCCESS"
            )
            logger.warning(f"MOCK: PhonePe redirect for ₹{amount}: {mock_url}")
            return {"redirect_url": mock_url, "transaction_id": transaction_id, "amount": amount_paise}

        pay_payload = {
            "merchantOrderId": transaction_id,
            "amount": amount_paise,
            "expireAfter": 900,
            "paymentFlow": {
                "type": "PG_CHECKOUT",
                "message": "TS Boat Tourism booking",
                "merchantUrls": {"redirectUrl": redirect_url},
            },
        }

        token = await self._get_oauth_token()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"O-Bearer {token}",
        }
        if self.merchant_id:
            headers["X-MERCHANT-ID"] = self.merchant_id

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{self.base_url}/checkout/v2/pay",
                    json=pay_payload,
                    headers=headers,
                )

            try:
                res_json = response.json()
            except ValueError:
                raw = response.text.strip()
                logger.error(f"PhonePe non-JSON response [{response.status_code}]: {raw}")
                detail = (
                    "PhonePe Error R016: Callback URL not whitelisted. Whitelist it in PhonePe Dashboard."
                    if raw == "R016"
                    else f"PhonePe gateway error: {raw}"
                )
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

            if response.status_code == 200:
                redirect = res_json.get("redirectUrl")
                if redirect:
                    logger.info(f"PhonePe payment initiated for txn {transaction_id} — ₹{amount}")
                    return {"redirect_url": redirect, "transaction_id": transaction_id, "amount": amount_paise}

            err_msg = res_json.get("message") or res_json.get("error") or "Failed to generate payment link."
            logger.error(f"PhonePe Pay API error [{response.status_code}]: {response.text[:300]}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"PhonePe gateway error: {err_msg}",
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"PhonePe HTTP connection failed: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Connection to payment gateway failed.",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Payment Status Check (polling / verify-status endpoint)
    # ─────────────────────────────────────────────────────────────────────────

    async def get_transaction_status(self, transaction_id: str) -> dict:
        """Query PhonePe V2 API to check payment status."""
        if self.is_mock:
            if transaction_id.startswith("fail_"):
                return {"status": "FAILED", "gateway_payment_id": None}
            return {"status": "SUCCESS", "gateway_payment_id": f"pay_mock_{transaction_id}"}

        token = await self._get_oauth_token()
        headers = {"Content-Type": "application/json", "Authorization": f"O-Bearer {token}"}
        if self.merchant_id:
            headers["X-MERCHANT-ID"] = self.merchant_id

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{self.base_url}/checkout/v2/order/{transaction_id}/status"
                    "?details=true&errorContext=true",
                    headers=headers,
                )

            res_json = response.json()
            if response.status_code == 200:
                state = str(res_json.get("state") or "").upper()
                gateway_payment_id = None
                payment_details = res_json.get("paymentDetails", [])
                if isinstance(payment_details, list):
                    for detail in payment_details:
                        det_status = str(detail.get("status") or detail.get("state") or "").upper()
                        if det_status in ("SUCCESS", "COMPLETED"):
                            gateway_payment_id = (
                                detail.get("transactionId")
                                or detail.get("pgTransactionId")
                                or detail.get("cfTransactionId")
                            )
                            break
                    if not gateway_payment_id and payment_details:
                        gateway_payment_id = (
                            payment_details[0].get("transactionId")
                            or payment_details[0].get("pgTransactionId")
                        )

                if state == "COMPLETED":
                    return {"status": "SUCCESS", "gateway_payment_id": gateway_payment_id or res_json.get("orderId") or transaction_id}
                elif state == "PENDING":
                    return {"status": "PENDING", "gateway_payment_id": gateway_payment_id}
                else:
                    return {"status": "FAILED", "gateway_payment_id": gateway_payment_id}

            logger.warning(f"PhonePe status query error for {transaction_id}: {response.text[:200]}")
            return {"status": "PENDING", "gateway_payment_id": None}
        except Exception as exc:
            logger.error(f"PhonePe status check failed: {exc}")
            return {"status": "PENDING", "gateway_payment_id": None}

    # ─────────────────────────────────────────────────────────────────────────
    # Webhook Signature Verification
    # PhonePe v2 supports two webhook auth modes:
    #   1. HMAC  → headers: x-phonepe-checksum-signature + x-phonepe-checksum-key-id
    #   2. SHA   → header: x-verify (legacy v1 format)
    # We verify both so the endpoint works regardless of which mode is set in dashboard.
    # ─────────────────────────────────────────────────────────────────────────

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        x_verify: Optional[str] = None,
        x_hmac_signature: Optional[str] = None,
        x_hmac_key_id: Optional[str] = None,
        auth_header: Optional[str] = None,
        # legacy base64 response parameter (kept for backward compat callers)
        base64_response: Optional[str] = None,
    ) -> bool:
        """Verify incoming PhonePe webhook authenticity.

        Priority order:
        1. HMAC (x-phonepe-checksum-signature)  — recommended by PhonePe v2 dashboard
        2. SHA Basic Auth (Authorization)        — dashboard SHA option
        3. SHA x-verify                         — legacy format still used by some accounts
        4. MOCK mode                             — always returns True
        """
        if self.is_mock:
            return True

        # ── 1. HMAC mode ──────────────────────────────────────────────────────
        if x_hmac_signature:
            # Use salt_key as the HMAC secret (stored as PHONEPE_SALT_KEY env var)
            # or fall back to client_secret if salt_key is not configured
            hmac_secret = self.salt_key or self.client_secret
            if not hmac_secret:
                logger.error("PhonePe HMAC verification: no secret key configured (PHONEPE_SALT_KEY / PHONEPE_CLIENT_SECRET)")
                return False
            try:
                computed = hmac.new(
                    key=hmac_secret.encode("utf-8"),
                    msg=raw_body,
                    digestmod=hashlib.sha256,
                ).hexdigest()
                result = hmac.compare_digest(computed.lower(), x_hmac_signature.strip().lower())
                if not result:
                    logger.warning(
                        f"PhonePe HMAC mismatch. KeyID={x_hmac_key_id} "
                        f"Expected={computed[:16]}… Got={x_hmac_signature[:16]}…"
                    )
                return result
            except Exception as exc:
                logger.error(f"PhonePe HMAC verification error: {exc}")
                return False

        # ── 2. SHA / Basic Auth mode ──────────────────────────────────────────
        if auth_header:
            wh_user = settings.PHONEPE_WEBHOOK_USERNAME
            wh_pass = settings.PHONEPE_WEBHOOK_PASSWORD
            if wh_user and wh_pass:
                try:
                    auth_clean = auth_header.strip()
                    # Check Basic auth
                    if auth_clean.startswith("Basic "):
                        expected_basic = "Basic " + base64.b64encode(f"{wh_user}:{wh_pass}".encode("utf-8")).decode("utf-8")
                        if hmac.compare_digest(expected_basic, auth_clean):
                            return True
                    # Check SHA256(username:password)
                    expected_sha = hashlib.sha256(f"{wh_user}:{wh_pass}".encode("utf-8")).hexdigest()
                    if hmac.compare_digest(expected_sha.lower(), auth_clean.lower()):
                        return True
                    # Check SHA256(username:password:raw_body)
                    expected_body_sha = hashlib.sha256(f"{wh_user}:{wh_pass}".encode("utf-8") + raw_body).hexdigest()
                    if hmac.compare_digest(expected_body_sha.lower(), auth_clean.lower()):
                        return True
                except Exception as exc:
                    logger.error(f"PhonePe SHA auth verification error: {exc}")
                    return False

        # ── 3. SHA x-verify (legacy) ──────────────────────────────────────────
        sig = x_verify
        b64 = base64_response
        if sig and b64:
            # Use salt_key if present; otherwise client_secret
            secret = self.salt_key or self.client_secret
            if not secret:
                logger.error("PhonePe x-verify: no secret configured (PHONEPE_SALT_KEY / PHONEPE_CLIENT_SECRET)")
                return False
            try:
                string_to_hash = b64 + secret
                hashed = hashlib.sha256(string_to_hash.encode("utf-8")).hexdigest()
                expected = f"{hashed}###{self.salt_index}"
                result = hmac.compare_digest(expected.lower(), sig.strip().lower())
                if not result:
                    logger.warning(
                        f"PhonePe x-verify mismatch. Expected={expected[:20]}… Got={sig[:20]}…"
                    )
                return result
            except Exception as exc:
                logger.error(f"PhonePe x-verify error: {exc}")
                return False

        # No signature headers present at all
        logger.warning("PhonePe webhook received with no recognized signature headers.")
        return False


phonepe_service = PhonePeService()
