from sqlalchemy import Column, String, Numeric, Integer, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseModel

class Coupon(BaseModel):
    __tablename__ = "coupons"

    code = Column(String(32), unique=True, index=True, nullable=False)
    discount_type = Column(String(16), nullable=False) # 'FLAT' or 'PERCENTAGE'
    discount_value = Column(Numeric(10, 2), nullable=False)
    min_booking_amount = Column(Numeric(10, 2), nullable=True)
    max_discount_amount = Column(Numeric(10, 2), nullable=True) # for percentage max cap
    usage_limit = Column(Integer, nullable=True)
    usage_count = Column(Integer, default=0, nullable=False)
    package_id = Column(Integer, ForeignKey("packages.id", ondelete="SET NULL"), nullable=True) # RESTRICT TO SPECIFIC PACKAGE
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_until = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    # Relationship to package (optional)
    package = relationship("Package", backref="coupons")

    def is_valid(self, booking_amount: float = 0.0, package_id: int = None) -> bool:
        """
        Evaluate if this coupon is active and valid for redemption.
        Enforces:
        1. is_active == True
        2. deleted_at is None
        3. valid_from <= now (if set)
        4. valid_until >= now (if set)
        5. usage_count < usage_limit (if usage_limit set)
        6. booking_amount >= min_booking_amount (if min_booking_amount set)
        7. package_id matches (if package_id set)
        """
        from app.core.timezone import get_ist_now
        
        # 1. Active & deleted checks
        if not self.is_active or getattr(self, 'deleted_at', None) is not None:
            return False
            
        # 2. Validity date parameters check
        now = get_ist_now()
        if self.valid_from and self.valid_from > now:
            return False
        if self.valid_until and self.valid_until < now:
            return False
            
        # 3. Redemptions cap check
        if self.usage_limit is not None and self.usage_count >= self.usage_limit:
            return False
            
        # 4. Booking amount threshold check
        if self.min_booking_amount is not None and booking_amount < float(self.min_booking_amount):
            return False
            
        # 5. Target product constraints check
        if self.package_id is not None and (package_id is None or package_id != self.package_id):
            return False
            
        return True
