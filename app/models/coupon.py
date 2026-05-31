from sqlalchemy import Column, String, Numeric, Integer, DateTime, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import relationship
from app.models.base import BaseModel

class Coupon(BaseModel):
    __tablename__ = "coupons"

    code = Column(String(32), unique=True, index=True, nullable=False)
    discount_type = Column(String(16), nullable=False) # 'FLAT' or 'PERCENTAGE'
    discount_value = Column(Numeric(10, 2), nullable=False)
    min_booking_amount = Column(Numeric(10, 2), nullable=True)
    max_discount_amount = Column(Numeric(10, 2), nullable=True) # for percentage max cap
    min_tickets = Column(Integer, nullable=True)
    usage_limit = Column(Integer, nullable=True)
    usage_count = Column(Integer, default=0, nullable=False)
    applicable_package_ids = Column(ARRAY(Integer), default=[], nullable=False, server_default='{}')
    applicable_room_ids = Column(ARRAY(Integer), default=[], nullable=False, server_default='{}')
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_until = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    def is_valid(self, booking_amount: float = 0.0, target_type: str = None, target_id: int = None, ticket_count: int = 0) -> bool:
        """
        Evaluate if this coupon is active and valid for redemption.
        Enforces:
        1. is_active == True
        2. deleted_at is None
        3. valid_from <= now (if set)
        4. valid_until >= now (if set)
        5. usage_count < usage_limit (if usage_limit set)
        6. booking_amount >= min_booking_amount (if min_booking_amount set)
        7. ticket_count >= min_tickets (if min_tickets set)
        8. package_id matches (if package_id set)
        """
        from app.core.timezone import get_ist_now
        
        # 1. Active & deleted checks
        if not self.is_active or getattr(self, 'deleted_at', None) is not None:
            return False
            
        # 2. Validity date parameters check
        now = get_ist_now()
        if self.valid_from and self.valid_from > now:
            return False
        if self.valid_until:
            val_until = self.valid_until
            if val_until.hour == 0 and val_until.minute == 0 and val_until.second == 0:
                val_until = val_until.replace(hour=23, minute=59, second=59, microsecond=999999)
            if val_until < now:
                return False
            
        # 3. Redemptions cap check
        if self.usage_limit is not None and self.usage_count >= self.usage_limit:
            return False
            
        # 4. Booking amount threshold check
        if self.min_booking_amount is not None and booking_amount < float(self.min_booking_amount):
            return False
            
        # 5. Minimum tickets check
        if self.min_tickets is not None and ticket_count < self.min_tickets:
            return False
            
        # 6. Target product constraints check
        is_global = not self.applicable_package_ids and not self.applicable_room_ids
        if is_global:
            pass # Applies to all
        else:
            if target_type == 'PACKAGE':
                if target_id not in (self.applicable_package_ids or []):
                    return False
            elif target_type == 'ROOM':
                if target_id not in (self.applicable_room_ids or []):
                    return False
            else:
                return False # Unknown target_type and coupon is restricted
            
        return True

    def calculate_discount(self, booking_amount: float) -> float:
        """
        Calculate the exact discount amount for a given booking subtotal.
        Never returns negative values. Applies max_discount_amount cap if PERCENTAGE.
        """
        if booking_amount <= 0:
            return 0.0
            
        discount = 0.0
        
        if self.discount_type == 'FLAT':
            discount = min(float(self.discount_value), booking_amount)
        elif self.discount_type == 'PERCENTAGE':
            discount = booking_amount * (float(self.discount_value) / 100.0)
            if self.max_discount_amount is not None:
                discount = min(discount, float(self.max_discount_amount))
                
        return max(0.0, discount)
