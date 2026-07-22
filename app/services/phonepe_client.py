import base64
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
        self.env = settings.PHONEPE_ENV or "SANDBOX"

        if self.env.upper() in ("PROD", "PRODUCTION"):
            self.base_url = "https://api.phonepe.com/apis/pg"
            self.oauth_url = "https://api.phonepe.com/apis/identity-manager/v1/oauth/token"
        else:
            self.base_url = "https://api-preprod.phonepe.com/apis/pg-sandbox"
            self.oauth_url = "https://api-preprod.phonepe.com/apis/pg-sandbox/v1/oauth/token"

        if self.env.upper() == "MOCK" or not self.client_id or not self.client_secret:
            logger.warning("PhonePe Service initialized in MOCK mode. Mocking payment gateway.")
            self.is_mock = True
        else:
            self.is_mock = False
            logger.info(f"PhonePe Service initialized in {self.env} mode. Base URL: {self.base_url}")

        self._cached_token = None
        self._token_expires_at = 0.0

    async def _get_oauth_token(self) -> str:
        """
        Fetches a fresh access token using Client credentials.
        """
        if self._cached_token and time.time() < self._token_expires_at - 60:
            return self._cached_token

        token_payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "client_version": self.client_version,
            "grant_type": "client_credentials"
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.oauth_url,
                    data=token_payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=15.0
                )
            
            if response.status_code == 200:
                token_data = response.json()
                token = token_data.get("access_token")
                expires_in = token_data.get("expires_in", 900)
                if token:
                    self._cached_token = token
                    self._token_expires_at = time.time() + float(expires_in)
                    logger.info(f"PhonePe OAuth token successfully cached. Expires in {expires_in} seconds.")
                    return token
            logger.error(f"Failed to fetch PhonePe OAuth token: {response.status_code} {response.text}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to authenticate with payment provider."
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Exception during PhonePe OAuth: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connection to payment authentication server failed."
            )

    async def create_payment_url(
        self,
        amount: float,
        transaction_id: str,
        user_id: str,
        redirect_url: str,
        callback_url: str,
        phone_number: Optional[str] = None
    ) -> dict:
        """
        Creates a PhonePe V2 payment session and returns the redirect URL.
        """
        amount_paise = int(round(amount * 100))

        if self.is_mock:
            mock_url = f"{settings.FRONTEND_URL}/payment-status?merchantTransactionId={transaction_id}&merchantId=MOCK&code=PAYMENT_SUCCESS"
            logger.warning(f"Mocking PhonePe redirect URL for {amount} INR: {mock_url}")
            return {
                "redirect_url": mock_url,
                "transaction_id": transaction_id,
                "amount": amount_paise
            }

        # V2 Payload
        pay_payload = {
            "merchantOrderId": transaction_id,
            "amount": amount_paise,
            "expireAfter": 900,
            "paymentFlow": {
                "type": "PG_CHECKOUT",
                "message": "Telangana Boat Tourism booking",
                "merchantUrls": {
                    "redirectUrl": redirect_url
                }
            }
        }

        token = await self._get_oauth_token()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"O-Bearer {token}",
            # "X-CALLBACK-URL": callback_url,
            # "X-CALL-MODE": "POST"
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/checkout/v2/pay",
                    json=pay_payload,
                    headers=headers,
                    timeout=15.0
                )
            
            try:
                res_json = response.json()
            except ValueError:
                logger.error(f"PhonePe Pay API responded with non-JSON: {response.status_code} {response.text}")
                error_msg = response.text.strip()
                if error_msg == "R016":
                    detail_msg = "PhonePe Error R016: Callback URL is not whitelisted. Please whitelist the callback URL in PhonePe Dashboard."
                else:
                    detail_msg = f"PhonePe gateway error: {error_msg}"
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=detail_msg
                )

            if response.status_code == 200:
                redirect_url_from_api = res_json.get("redirectUrl")
                if redirect_url_from_api:
                    return {
                        "redirect_url": redirect_url_from_api,
                        "transaction_id": transaction_id,
                        "amount": amount_paise
                    }
                
            logger.error(f"PhonePe Pay API responded with error: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"PhonePe gateway error: {res_json.get('message', 'Failed to generate payment link.')}"
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"HTTP Connection to PhonePe failed: {str(exc)}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connection to payment gateway failed."
            )

    async def get_transaction_status(self, transaction_id: str) -> dict:
        """
        Queries PhonePe V2 API to check payment status.
        """
        if self.is_mock:
            if transaction_id.startswith("fail_"):
                return {"status": "FAILED", "gateway_payment_id": None}
            return {"status": "SUCCESS", "gateway_payment_id": f"pay_mock_{transaction_id}"}

        token = await self._get_oauth_token()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"O-Bearer {token}"
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/checkout/v2/order/{transaction_id}/status?details=true&errorContext=true",
                    headers=headers,
                    timeout=10.0
                )
            
            res_json = response.json()
            if response.status_code == 200:
                state = res_json.get("state")
                
                # Retrieve transaction ID/payment ID if completed
                gateway_payment_id = None
                payment_details = res_json.get("paymentDetails", [])
                if payment_details and isinstance(payment_details, list):
                    for detail in payment_details:
                        if detail.get("status") == "SUCCESS":
                            gateway_payment_id = detail.get("pgTransactionId") or detail.get("cfTransactionId")
                            break
                    if not gateway_payment_id and len(payment_details) > 0:
                        gateway_payment_id = payment_details[0].get("pgTransactionId")

                if state == "COMPLETED":
                    return {"status": "SUCCESS", "gateway_payment_id": gateway_payment_id or transaction_id}
                elif state == "PENDING":
                    return {"status": "PENDING", "gateway_payment_id": gateway_payment_id}
                else:
                    return {"status": "FAILED", "gateway_payment_id": gateway_payment_id}
            
            logger.warning(f"PhonePe Status query returned error code for {transaction_id}: {response.text}")
            return {"status": "PENDING", "gateway_payment_id": None}
        except Exception as exc:
            logger.error(f"PhonePe Status check connection failed: {str(exc)}")
            return {"status": "PENDING", "gateway_payment_id": None}

    def verify_webhook_signature(self, base64_response: str, received_signature: str) -> bool:
        """
        Verify the signature of the webhook callback.
        """
        if self.is_mock:
            return True
            
        if not received_signature:
            return False
            
        import hashlib
        try:
            # Formula: SHA256(base64_response + salt_key) + "###" + salt_index
            string_to_hash = base64_response + self.client_secret
            hashed = hashlib.sha256(string_to_hash.encode('utf-8')).hexdigest()
            expected_signature = f"{hashed}###{self.client_version}"
            return expected_signature == received_signature
        except Exception as e:
            logger.error(f"Error verifying PhonePe webhook signature: {e}")
            return False

phonepe_service = PhonePeService()
