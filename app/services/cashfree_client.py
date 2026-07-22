"""
Cashfree Payment Gateway Client
Uses the Cashfree Python SDK (cashfree-pg) for production payments.
Supports: Create Order (Popup/Seamless), Verify Payment, Webhook Signature Verification.
"""
import hmac
import hashlib
import httpx
from loguru import logger
from fastapi import HTTPException, status
from app.core.config import settings


class CashfreeService:
    def __init__(self):
        self.app_id = settings.CASHFREE_APP_ID
        self.secret_key = settings.CASHFREE_SECRET_KEY
        self.env = settings.CASHFREE_ENV or "PRODUCTION"

        if self.env == "PRODUCTION":
            self.base_url = "https://api.cashfree.com/pg"
        else:
            self.base_url = "https://sandbox.cashfree.com/pg"

        if not self.app_id or not self.secret_key or self.env == "MOCK":
            logger.warning(f"Cashfree credentials not configured or env is {self.env}. Running in MOCK mode.")
            self._mock = True
        else:
            self._mock = False
            logger.info(f"CashfreeService initialized in {self.env} mode.")

    def _headers(self) -> dict:
        return {
            "x-client-id": self.app_id,
            "x-client-secret": self.secret_key,
            "x-api-version": "2023-08-01",
            "Content-Type": "application/json",
        }

    async def create_order(
        self,
        order_id: str,
        amount: float,
        customer_id: str,
        customer_name: str,
        customer_email: str,
        customer_phone: str,
        return_url: str,
    ) -> dict:
        """
        Creates a Cashfree order and returns the payment_session_id for the Cashfree JS popup.
        Returns: { order_id, payment_session_id, order_status }
        """
        amount_rounded = round(amount, 2)

        if self._mock:
            mock_session = f"mock_session_{order_id}"
            logger.warning(f"[MOCK] Cashfree order for {amount_rounded} INR. session={mock_session}")
            return {
                "order_id": order_id,
                "payment_session_id": mock_session,
                "order_status": "ACTIVE",
            }

        payload = {
            "order_id": order_id,
            "order_amount": amount_rounded,
            "order_currency": "INR",
            "customer_details": {
                "customer_id": customer_id[:50],  # Cashfree max 50 chars
                "customer_name": customer_name[:50] if customer_name else "Customer",
                "customer_email": customer_email or "noreply@tstelanganatourism.com",
                "customer_phone": customer_phone or "9999999999",
            },
            "order_meta": {
                "return_url": return_url,
                # Avoid sending empty notify_url to prevent validation errors
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self.base_url}/orders",
                    headers=self._headers(),
                    json=payload,
                )
                try:
                    data = resp.json()
                except ValueError:
                    logger.error(f"Cashfree Pay API responded with non-JSON: {resp.status_code} {resp.text}")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Cashfree gateway error: {resp.text.strip()}"
                    )

                if resp.status_code not in (200, 201):
                    logger.error(f"Cashfree create_order failed: {resp.status_code} {resp.text}")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Cashfree gateway error: {data.get('message', 'Order creation failed.')}",
                    )
                return {
                    "order_id": data.get("order_id"),
                    "payment_session_id": data.get("payment_session_id"),
                    "order_status": data.get("order_status"),
                }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Cashfree create_order exception: {exc}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to communicate with Cashfree payment gateway.",
            )

    async def get_order_status(self, order_id: str) -> dict:
        """
        Fetches order + payment status from Cashfree.
        Returns: { status: PAID | ACTIVE | EXPIRED, pg_payment_id }
        """
        if self._mock:
            logger.warning(f"[MOCK] Cashfree order status check for {order_id}")
            return {"status": "PAID", "pg_payment_id": f"mock_pay_{order_id}"}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{self.base_url}/orders/{order_id}",
                    headers=self._headers(),
                )
                if resp.status_code != 200:
                    logger.error(f"Cashfree get_order_status failed: {resp.status_code} {resp.text}")
                    return {"status": "ERROR", "pg_payment_id": None}

                try:
                    data = resp.json()
                except ValueError:
                    logger.error(f"Cashfree get_order_status returned non-JSON: {resp.text}")
                    return {"status": "ERROR", "pg_payment_id": None}

                order_status = data.get("order_status", "ACTIVE")  # PAID | ACTIVE | EXPIRED | CANCELLED

                # Fetch the payment details if paid
                pg_payment_id = None
                if order_status == "PAID":
                    pay_resp = await client.get(
                        f"{self.base_url}/orders/{order_id}/payments",
                        headers=self._headers(),
                    )
                    if pay_resp.status_code == 200:
                        try:
                            payments = pay_resp.json()
                        except ValueError:
                            logger.error(f"Cashfree payments API returned non-JSON: {pay_resp.text}")
                            payments = []
                        if payments and isinstance(payments, list):
                            # Get the latest successful payment
                            for p in payments:
                                if p.get("payment_status") == "SUCCESS":
                                    pg_payment_id = p.get("cf_payment_id") or p.get("payment_id")
                                    break

                return {"status": order_status, "pg_payment_id": str(pg_payment_id) if pg_payment_id else None}
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Cashfree get_order_status exception: {exc}")
            return {"status": "ERROR", "pg_payment_id": None}

    def verify_webhook_signature(self, timestamp: str, raw_body: str, signature: str) -> bool:
        """
        Verify Cashfree webhook payload signature.
        Cashfree signs: timestamp + raw_body using HMAC-SHA256 with the secret key.
        """
        if self._mock:
            logger.warning("Skipping Cashfree webhook signature verification (MOCK mode)")
            return True

        if not self.secret_key or not signature:
            return False

        try:
            message = f"{timestamp}{raw_body}"
            expected = hmac.new(
                key=self.secret_key.encode("utf-8"),
                msg=message.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).hexdigest()
            # Cashfree sends base64 — but some versions send hex. Support both.
            import base64
            try:
                expected_b64 = base64.b64encode(
                    hmac.new(
                        key=self.secret_key.encode("utf-8"),
                        msg=message.encode("utf-8"),
                        digestmod=hashlib.sha256,
                    ).digest()
                ).decode("utf-8")
                return hmac.compare_digest(expected_b64, signature)
            except Exception:
                return hmac.compare_digest(expected, signature)
        except Exception as exc:
            logger.error(f"Cashfree webhook signature verification failed: {exc}")
            return False


cashfree_service = CashfreeService()
