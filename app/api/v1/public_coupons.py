from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal

from app.db.session import get_db
from app.models.coupon import Coupon
from app.schemas.coupon import CouponValidateRequest, CouponValidateResponse
from app.core.timezone import get_ist_now

router = APIRouter(
    prefix="/coupons",
    tags=["Public Discovery - Coupons"]
)

@router.post("/validate", response_model=CouponValidateResponse)
async def validate_coupon(
    body: CouponValidateRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Public endpoint to validate a coupon and calculate discount.
    """
    code_upper = body.code.upper().strip()
    
    query = select(Coupon).where(Coupon.code == code_upper, Coupon.deleted_at.is_(None))
    result = await db.execute(query)
    coupon = result.scalar_one_or_none()
    
    if not coupon:
        return CouponValidateResponse(
            valid=False,
            reason="Coupon not found or invalid"
        )
        
    is_valid = coupon.is_valid(booking_amount=body.booking_amount, target_type=body.target_type, target_id=body.target_id)
    if not is_valid:
        # Determine specific reason for UX
        reason = "Coupon criteria not met"
        now = get_ist_now()
        if not coupon.is_active:
            reason = "Coupon is inactive"
        elif coupon.valid_until and coupon.valid_until < now:
            reason = "Coupon has expired"
        elif coupon.valid_from and coupon.valid_from > now:
            reason = "Coupon is not yet valid"
        elif coupon.usage_limit is not None and coupon.usage_count >= coupon.usage_limit:
            reason = "Coupon usage limit reached"
        elif coupon.min_booking_amount is not None and body.booking_amount < float(coupon.min_booking_amount):
            reason = f"Minimum booking amount of ₹{coupon.min_booking_amount} required"
        elif body.target_type == 'PACKAGE' and coupon.applicable_package_ids and body.target_id not in coupon.applicable_package_ids:
            reason = "Coupon is not applicable for this package"
        elif body.target_type == 'ROOM' and coupon.applicable_room_ids and body.target_id not in coupon.applicable_room_ids:
            reason = "Coupon is not applicable for this room"
            
        return CouponValidateResponse(
            valid=False,
            reason=reason
        )
        
    discount = float(coupon.calculate_discount(body.booking_amount))
    
    return CouponValidateResponse(
        valid=True,
        discount_amount=discount,
        discounted_subtotal=max(0.0, body.booking_amount - discount)
    )

# =====================================================================
# BOOKING SAFETY - FUTURE REDEMPTION IMPLEMENTATION GUIDE
# =====================================================================
# When implementing the actual booking/checkout endpoint, you MUST use 
# row-level locking to prevent concurrency issues and double-use exploits.
# 
# Example snippet for future implementation:
# 
# async def apply_coupon_and_book(db: AsyncSession, code: str, ...):
#     # 1. Lock the row to prevent race conditions during redemption
#     query = select(Coupon).where(Coupon.code == code).with_for_update()
#     result = await db.execute(query)
#     coupon = result.scalar_one_or_none()
# 
#     # 2. Re-evaluate coupon.is_valid() strictly under the lock
#     if not coupon or not coupon.is_valid(...):
#         raise HTTPException(...)
# 
#     # 3. Safely increment the usage tracker
#     coupon.usage_count += 1
#     
#     # 4. Create booking, apply discount, then commit transaction
#     await db.commit()
# =====================================================================
