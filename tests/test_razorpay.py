import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hmac
import hashlib
from app.services.razorpay_client import razorpay_service
from app.core.config import settings

def test_signature_verification():
    # Setup test secrets
    settings.RAZORPAY_KEY_SECRET = "test_secret_123"
    razorpay_service.client = True  # Mock client presence

    order_id = "order_123"
    payment_id = "pay_456"
    
    # Generate valid signature
    valid_signature = hmac.new(
        bytes(settings.RAZORPAY_KEY_SECRET, 'utf-8'),
        msg=bytes(order_id + "|" + payment_id, 'utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()

    # Should succeed
    assert razorpay_service.verify_signature(order_id, payment_id, valid_signature) is True
    
    # Should fail
    assert razorpay_service.verify_signature(order_id, payment_id, "invalid_sig") is False
    assert razorpay_service.verify_signature("wrong_order", payment_id, valid_signature) is False

    print("Razorpay Signature Unit Test: PASSED")

if __name__ == "__main__":
    test_signature_verification()
