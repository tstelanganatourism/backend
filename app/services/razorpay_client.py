import hmac
import hashlib
import razorpay
from loguru import logger
from fastapi import HTTPException, status
from app.core.config import settings

class RazorpayService:
    def __init__(self):
        if settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET:
            self.client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
        else:
            self.client = None
            logger.warning("Razorpay credentials not found in environment. Mocking gateway.")

    def create_order(self, amount: float, receipt: str, notes: dict = None) -> dict:
        """
        Creates a Razorpay Order.
        amount: Amount in standard currency (INR). It will be multiplied by 100 for paise.
        """
        amount_paise = int(round(amount * 100))
        
        if not self.client:
            logger.warning(f"Mocking Razorpay Order for {amount_paise} paise")
            return {
                "id": f"order_mock_{receipt}",
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
                "status": "created"
            }

        try:
            order_data = {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
                "payment_capture": 1 # Auto capture
            }
            if notes:
                order_data["notes"] = notes
                
            order = self.client.order.create(data=order_data)
            return order
        except Exception as e:
            logger.error(f"Razorpay Order Creation Failed: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to communicate with payment gateway."
            )

    def verify_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """
        Cryptographically verifies the Razorpay checkout signature.
        """
        if not self.client:
            # Accept all mock signatures starting with 'mock_sig'
            return signature.startswith("mock_sig_")

        try:
            expected_signature = hmac.new(
                key=settings.RAZORPAY_KEY_SECRET.encode('utf-8'),
                msg=f"{order_id}|{payment_id}".encode('utf-8'),
                digestmod=hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected_signature, signature)
        except Exception as e:
            logger.error(f"Razorpay Signature Verification Failed: {str(e)}")
            return False

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        """
        Verify Razorpay webhook payload HMAC signature.
        Uses the RAZORPAY_KEY_SECRET as the webhook secret.
        """
        if not self.client or not settings.RAZORPAY_KEY_SECRET:
            logger.warning("Skipping webhook signature verification (no credentials)")
            return True  # Allow in dev mode

        try:
            expected = hmac.new(
                key=settings.RAZORPAY_KEY_SECRET.encode('utf-8'),
                msg=body,
                digestmod=hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, signature or "")
        except Exception as e:
            logger.error(f"Webhook signature verification failed: {str(e)}")
            return False

    def refund_payment(self, payment_id: str, amount: float = None, notes: dict = None) -> dict:
        """
        Refunds a Razorpay payment.
        If amount is provided, performs a partial refund (amount in standard currency INR).
        If amount is None, performs a full refund.
        """
        if not self.client:
            logger.warning(f"Mocking Razorpay Refund for payment {payment_id} (amount: {amount})")
            return {
                "id": f"rfnd_mock_{payment_id}",
                "entity": "refund",
                "amount": int(round(amount * 100)) if amount is not None else 0,
                "currency": "INR",
                "payment_id": payment_id,
                "status": "processed"
            }

        try:
            refund_data = {}
            if amount is not None:
                refund_data["amount"] = int(round(amount * 100)) # in paise
            if notes:
                refund_data["notes"] = notes
                
            refund_data["payment_id"] = payment_id
            refund = self.client.refund.create(data=refund_data)
            return refund
        except Exception as e:
            logger.error(f"Razorpay Refund Failed: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to communicate with payment gateway to execute refund."
            )

razorpay_service = RazorpayService()