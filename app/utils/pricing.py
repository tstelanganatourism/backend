from decimal import Decimal
from typing import Optional, Tuple

def get_effective_package_prices(
    base_adult_price: Decimal,
    base_child_price: Decimal,
    price_override: Optional[Decimal]
) -> Tuple[Decimal, Decimal]:
    """
    Computes effective adult and child package prices.
    Price override is treated as an additive modifier.
    Override can be positive or negative.
    Effective price must never go below zero.
    """
    modifier = price_override if price_override is not None else Decimal("0.00")
    
    effective_adult = max(Decimal("0.00"), base_adult_price + modifier)
    effective_child = max(Decimal("0.00"), base_child_price + modifier)
    
    return effective_adult, effective_child

def get_booking_hash(public_id: str, secret_key: str) -> str:
    """
    Generates a secure HMAC-SHA256 signature for a booking using the secret key.
    Used to secure printable ticket/invoice pages from brute-forcing.
    """
    import hmac
    import hashlib
    return hmac.new(
        secret_key.encode("utf-8"),
        public_id.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

