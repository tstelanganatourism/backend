"""
Promotions API router — public banner + admin CRUD.

Public:
  GET /active  → active, non-expired promotions (cached 60s)

Admin (requires ADMIN role):
  GET    /          → all promotions including inactive
  POST   /          → create promotion
  PATCH  /{id}      → update promotion
  DELETE /{id}      → soft-delete promotion
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.middleware.auth import require_admin
from app.repositories.promotion_repository import (
    create_promotion,
    get_active_promotions,
    get_all_promotions_admin,
    get_promotion_by_id,
    soft_delete_promotion,
    update_promotion,
)
from app.schemas.promotion import (
    PromotionAdminCreate,
    PromotionAdminResponse,
    PromotionAdminUpdate,
    PromotionPublicResponse,
)

router = APIRouter()


from app.models.coupon import Coupon
from app.core.timezone import get_ist_now
from app.models.enums import PromotionType, PromotionTarget, PromotionBadge
from sqlalchemy import select

# ─── Public ───────────────────────────────────────────────────────────────────

@router.get("/active", response_model=List[PromotionPublicResponse])
async def get_active_promotions_public(db: AsyncSession = Depends(get_db)):
    """
    Return all currently active promotions for the public scrolling banner.
    Automatically excludes expired and inactive promotions.
    Also merges and displays active discount coupons as scrollable promotions!
    """
    promotions = await get_active_promotions(db)
    public_promos = [PromotionPublicResponse.model_validate(p) for p in promotions]

    try:
        now = get_ist_now()
        coupon_query = select(Coupon).where(
            Coupon.is_active == True,
            Coupon.deleted_at.is_(None)
        ).where(
            (Coupon.valid_from.is_(None) | (Coupon.valid_from <= now)) &
            (Coupon.valid_until.is_(None) | (Coupon.valid_until >= now))
        )
        result = await db.execute(coupon_query)
        active_coupons = result.scalars().all()

        for c in active_coupons:
            discount_type = (
                PromotionType.PERCENT_DISCOUNT
                if c.discount_type == 'PERCENTAGE'
                else PromotionType.FLAT_DISCOUNT
            )
            target_type = (
                PromotionTarget.SPECIFIC_PACKAGES
                if c.package_id
                else PromotionTarget.ALL
            )
            
            disc_label = f"{float(c.discount_value)}% Off" if c.discount_type == 'PERCENTAGE' else f"₹{int(c.discount_value)} Off"
            subtitle = f"Use code {c.code} at checkout to save {disc_label}!"
            if c.min_booking_amount:
                subtitle += f" Min booking: ₹{int(c.min_booking_amount)}."

            coupon_promo = PromotionPublicResponse(
                id=100000 + c.id,
                title=f"PROMO CODE: {c.code}",
                subtitle=subtitle,
                icon_emoji="🎟️",
                badge=PromotionBadge.LIMITED_TIME,
                type=discount_type,
                target=target_type,
                discount_value=float(c.discount_value),
                cta_label="Copy Code & Book",
                cta_url=f"/tours/{c.package_id}" if c.package_id else "/tours",
                bg_gradient="from-emerald-600 to-teal-800",
                sort_order=50
            )
            public_promos.append(coupon_promo)
    except Exception as e:
        # Fallback to avoid breaking public API in case of migration mismatches
        pass

    # Sort public_promos by sort_order
    public_promos.sort(key=lambda x: x.sort_order)

    response = JSONResponse(
        content=[p.model_dump(mode="json") for p in public_promos]
    )
    response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=30"
    return response


# ─── Admin ────────────────────────────────────────────────────────────────────

@router.get("", response_model=List[PromotionAdminResponse])
async def list_all_promotions(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    """Admin: list all promotions including inactive ones."""
    promotions = await get_all_promotions_admin(db)
    return [PromotionAdminResponse.model_validate(p) for p in promotions]


@router.post("", response_model=PromotionAdminResponse, status_code=status.HTTP_201_CREATED)
async def create_promotion_admin(
    body: PromotionAdminCreate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    """Admin: create a new promotion."""
    promo = await create_promotion(db, body.model_dump())
    return PromotionAdminResponse.model_validate(promo)


@router.patch("/{promotion_id}", response_model=PromotionAdminResponse)
async def update_promotion_admin(
    promotion_id: int,
    body: PromotionAdminUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    """Admin: update any fields of an existing promotion."""
    promo = await get_promotion_by_id(db, promotion_id)
    if not promo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promotion not found.")

    updated = await update_promotion(
        db, promo, body.model_dump(exclude_unset=True)
    )
    return PromotionAdminResponse.model_validate(updated)


@router.delete("/{promotion_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_promotion_admin(
    promotion_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_admin),
):
    """Admin: soft-delete a promotion (sets deleted_at timestamp)."""
    promo = await get_promotion_by_id(db, promotion_id)
    if not promo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promotion not found.")
    await soft_delete_promotion(db, promo)
