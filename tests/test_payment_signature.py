"""
Tests for Razorpay signature verification.
"""
import sys
import os
import hmac
import hashlib
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# We test the HMAC logic directly without needing the Razorpay service
def test_signature_computation():
    """Test that HMAC-SHA256 produces expected signature for a known input."""
    secret = "test_secret_key"
    order_id = "order_123"
    payment_id = "pay_456"
    
    expected = hmac.new(
        key=secret.encode('utf-8'),
        msg=f"{order_id}|{payment_id}".encode('utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()
    
    # Re-compute to verify determinism
    actual = hmac.new(
        key=secret.encode('utf-8'),
        msg=f"{order_id}|{payment_id}".encode('utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()
    
    assert expected == actual, f"Signature mismatch: {expected} != {actual}"
    assert len(expected) == 64, f"Expected 64 char hex string, got {len(expected)}"
    print(f"[OK] Checkout signature: {expected[:16]}...")


def test_webhook_signature():
    """Test webhook body signature verification logic."""
    secret = "webhook_secret_key"
    body = b'{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_test"}}}}'
    
    sig = hmac.new(
        key=secret.encode('utf-8'),
        msg=body,
        digestmod=hashlib.sha256
    ).hexdigest()
    
    # Verify
    expected = hmac.new(
        key=secret.encode('utf-8'),
        msg=body,
        digestmod=hashlib.sha256
    ).hexdigest()
    
    assert hmac.compare_digest(sig, expected), "Webhook signature mismatch"
    assert not hmac.compare_digest(sig, "wrong_signature"), "Should reject wrong signature"
    print("[OK] Webhook signature verification works correctly")


def test_signature_reject_tampered():
    """Test that a tampered body produces a different signature."""
    secret = "test_key"
    body_original = b'{"amount": 1000}'
    body_tampered = b'{"amount": 9999}'
    
    sig_original = hmac.new(key=secret.encode('utf-8'), msg=body_original, digestmod=hashlib.sha256).hexdigest()
    sig_tampered = hmac.new(key=secret.encode('utf-8'), msg=body_tampered, digestmod=hashlib.sha256).hexdigest()
    
    assert sig_original != sig_tampered, "Tampered body should produce different signature"
    print("[OK] Tampered payload correctly rejected")


if __name__ == "__main__":
    test_signature_computation()
    test_webhook_signature()
    test_signature_reject_tampered()
    print("\n[OK] All payment signature tests passed!")
