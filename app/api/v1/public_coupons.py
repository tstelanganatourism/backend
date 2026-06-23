from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal

from app.db.session import get_db
from app.models.coupon import Coupon
from app.schemas.coupon import CouponValidateRequest, CouponValidateResponse
from app.core.timezone import get_ist_now
from app.utils.cache import ttl_cache_get_or_set

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
        
    is_valid = coupon.is_valid(
        booking_amount=body.booking_amount, 
        target_type=body.target_type, 
        target_id=body.target_id,
        ticket_count=body.ticket_count,
        travel_date=body.travel_date
    )
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
        elif coupon.is_weekend_only and body.travel_date and body.travel_date.weekday() not in (5, 6):
            reason = "This coupon is only valid for weekend travel dates"
        elif coupon.usage_limit is not None and coupon.usage_count >= coupon.usage_limit:
            reason = "Coupon usage limit reached"
        elif coupon.min_tickets is not None and body.ticket_count < coupon.min_tickets:
            reason = f"Minimum {coupon.min_tickets} tickets/passengers required"
        elif coupon.min_booking_amount is not None and body.booking_amount < float(coupon.min_booking_amount):
            reason = f"Minimum booking amount of ₹{coupon.min_booking_amount} required"
        elif body.target_type == 'PACKAGE' and coupon.applicable_package_ids and body.target_id not in coupon.applicable_package_ids and -1 not in coupon.applicable_package_ids:
            reason = "Coupon is not applicable for this package"
        elif body.target_type == 'ROOM' and coupon.applicable_room_ids and body.target_id not in coupon.applicable_room_ids and -1 not in coupon.applicable_room_ids:
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

# Cache active coupons for 60 s — these change rarely and are called on every page load.
_COUPON_CACHE_TTL = 60

@router.get("/active")
async def get_active_coupons(
    target_type: str = None,
    target_id: int = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Public endpoint to get active coupons, optionally filtered by target.
    Cached in-process for 60 s to reduce live DB queries on page renders.
    """
    # Use target_type + target_id as part of the cache key for correct per-target caching.
    cache_key = f"coupons:active:{target_type}:{target_id}"

    async def _load():
        now = get_ist_now()
        query = select(Coupon).where(
            Coupon.is_active == True,
            Coupon.deleted_at.is_(None)
        )
        
        result = await db.execute(query)
        coupons = result.scalars().all()
        
        specific_coupons = []
        global_coupons = []
        for c in coupons:
            # Date range checks
            if c.valid_from and c.valid_from > now:
                continue
            val_until = c.valid_until
            if val_until and val_until.hour == 0 and val_until.minute == 0 and val_until.second == 0:
                val_until = val_until.replace(hour=23, minute=59, second=59, microsecond=999999)
            if val_until and val_until < now:
                continue

            is_global = not c.applicable_package_ids and not c.applicable_room_ids
            if is_global:
                global_coupons.append(c)
            elif target_type == 'PACKAGE' and target_id in (c.applicable_package_ids or []):
                specific_coupons.append(c)
            elif target_type == 'ROOM' and target_id in (c.applicable_room_ids or []):
                specific_coupons.append(c)

        valid_coupons = specific_coupons + global_coupons

        return [
            {
                "code": c.code,
                "discount_type": c.discount_type,
                "discount_value": float(c.discount_value),
                "min_booking_amount": float(c.min_booking_amount) if c.min_booking_amount else None,
                "max_discount_amount": float(c.max_discount_amount) if c.max_discount_amount else None,
                "min_tickets": c.min_tickets if c.min_tickets else None,
            }
            for c in valid_coupons
        ]

    data = await ttl_cache_get_or_set(cache_key, _COUPON_CACHE_TTL, _load)
    response = JSONResponse(content=data)
    # Coupons change rarely — allow a 60 s public cache with a 120 s stale window.
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60, stale-while-revalidate=120"
    return response


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
