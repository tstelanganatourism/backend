import base64
import hashlib
import json
from typing import Optional
import httpx
from loguru import logger
from fastapi import HTTPException, status
from app.core.config import settings

class PhonePeService:
    def __init__(self):
        self.merchant_id = settings.PHONEPE_MERCHANT_ID
        self.salt_key = settings.PHONEPE_SALT_KEY
        self.salt_index = settings.PHONEPE_SALT_INDEX
        self.env = settings.PHONEPE_ENV or "UAT"

        if self.env.upper() == "PROD":
            self.base_url = "https://api.phonepe.com/apis/hermes"
        else:
            self.base_url = "https://api-preprod.phonepe.com/apis/pg-sandbox"

        if self.env.upper() == "MOCK" or not self.merchant_id or not self.salt_key:
            logger.warning("PhonePe Service initialized in MOCK mode. Mocking payment gateway.")
            self.is_mock = True
        else:
            self.is_mock = False
            logger.info(f"PhonePe Service initialized in {self.env} mode. Base URL: {self.base_url}")

    def _generate_checksum(self, base64_payload: str, endpoint: str) -> str:
        """
        Formula: SHA256(base64EncodedPayload + API_ENDPOINT + SALT_KEY) + "###" + SALT_INDEX
        """
        string_to_hash = f"{base64_payload}{endpoint}{self.salt_key}"
        sha256_hash = hashlib.sha256(string_to_hash.encode('utf-8')).hexdigest()
        return f"{sha256_hash}###{self.salt_index}"

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
        Creates a PhonePe payment session and returns the redirect URL.
        amount: Amount in standard INR (float). Will be converted to Paise (int).
        """
        amount_paise = int(round(amount * 100))
        phone = (phone_number or "9999999999")[-10:] # Ensure 10 digits

        if self.is_mock:
            mock_url = f"{settings.FRONTEND_URL}/payment-status?merchantTransactionId={transaction_id}&merchantId=MOCK&code=PAYMENT_SUCCESS"
            logger.warning(f"Mocking PhonePe redirect URL for {amount} INR: {mock_url}")
            return {
                "redirect_url": mock_url,
                "transaction_id": transaction_id,
                "amount": amount_paise
            }

        # Construct payload
        payload = {
            "merchantId": self.merchant_id,
            "merchantTransactionId": transaction_id,
            "merchantUserId": f"USR_{user_id}",
            "amount": amount_paise,
            "redirectUrl": redirect_url,
            "redirectMode": "REDIRECT",
            "callbackUrl": callback_url,
            "mobileNumber": phone,
            "paymentInstrument": {
                "type": "PAY_PAGE"
            }
        }

        # Encode base64
        json_str = json.dumps(payload)
        base64_payload = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')

        # Checksum
        endpoint = "/pg/v1/pay"
        checksum = self._generate_checksum(base64_payload, endpoint)

        headers = {
            "Content-Type": "application/json",
            "X-VERIFY": checksum
        }

        request_body = {
            "request": base64_payload
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}{endpoint}",
                    json=request_body,
                    headers=headers,
                    timeout=15.0
                )
            
            res_json = response.json()
            if response.status_code == 200 and res_json.get("success") is True:
                data = res_json.get("data", {})
                redirect_url = data.get("instrumentResponse", {}).get("redirectInfo", {}).get("url")
                if redirect_url:
                    return {
                        "redirect_url": redirect_url,
                        "transaction_id": transaction_id,
                        "amount": amount_paise
                    }
                
            logger.error(f"PhonePe Pay API responded with error: {response.text}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"PhonePe gateway error: {res_json.get('message', 'Failed to generate payment link.')}"
            )
        except httpx.RequestError as exc:
            logger.error(f"HTTP Connection to PhonePe failed: {str(exc)}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Connection to payment gateway failed."
            )

    async def get_transaction_status(self, transaction_id: str) -> dict:
        """
        Queries the PhonePe API to check payment status.
        Returns a dict: {"status": "SUCCESS" | "PENDING" | "FAILED", "gateway_payment_id": str | None}
        """
        if self.is_mock:
            # For testing, you can check if mock transaction ends in certain ways
            if transaction_id.startswith("fail_"):
                return {"status": "FAILED", "gateway_payment_id": None}
            return {"status": "SUCCESS", "gateway_payment_id": f"pay_mock_{transaction_id}"}

        endpoint = f"/pg/v1/status/{self.merchant_id}/{transaction_id}"
        
        # Checksum calculation: SHA256(endpoint + salt_key) + "###" + salt_index
        string_to_hash = f"{endpoint}{self.salt_key}"
        sha256_hash = hashlib.sha256(string_to_hash.encode('utf-8')).hexdigest()
        checksum = f"{sha256_hash}###{self.salt_index}"

        headers = {
            "X-VERIFY": checksum,
            "X-MERCHANT-ID": self.merchant_id,
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}{endpoint}",
                    headers=headers,
                    timeout=10.0
                )
            
            res_json = response.json()
            if response.status_code == 200 and res_json.get("success") is True:
                code = res_json.get("code")
                data = res_json.get("data", {})
                gateway_payment_id = data.get("transactionId")
                
                # PhonePe Success codes
                if code == "PAYMENT_SUCCESS":
                    return {"status": "SUCCESS", "gateway_payment_id": gateway_payment_id}
                elif code == "PAYMENT_PENDING":
                    return {"status": "PENDING", "gateway_payment_id": gateway_payment_id}
                else:
                    return {"status": "FAILED", "gateway_payment_id": gateway_payment_id}
            
            # If code is PAYMENT_ERROR or response has failed
            logger.warning(f"PhonePe Status query returned error code for {transaction_id}: {response.text}")
            code = res_json.get("code")
            if code in ["INTERNAL_SERVER_ERROR", "BAD_REQUEST"]:
                # Gateway temporary errors: treat as PENDING so we retry rather than failing immediately
                return {"status": "PENDING", "gateway_payment_id": None}
            return {"status": "FAILED", "gateway_payment_id": None}
        except Exception as exc:
            logger.error(f"PhonePe Status check connection failed: {str(exc)}")
            # Return pending to avoid deleting draft on connection failures
            return {"status": "PENDING", "gateway_payment_id": None}

    def verify_webhook_signature(self, base64_response: str, received_signature: str) -> bool:
        """
        Verify the signature of the webhook callback.
        Formula: SHA256(base64_response + salt_key) + "###" + salt_index == received_signature
        """
        if self.is_mock:
            return True

        if not received_signature:
            return False

        string_to_hash = f"{base64_response}{self.salt_key}"
        sha256_hash = hashlib.sha256(string_to_hash.encode('utf-8')).hexdigest()
        expected_checksum = f"{sha256_hash}###{self.salt_index}"
        
        return hmac_compare(expected_checksum, received_signature)

def hmac_compare(val1: str, val2: str) -> bool:
    import hmac
    return hmac.compare_digest(val1.encode('utf-8'), val2.encode('utf-8'))

phonepe_service = PhonePeService()
