"""
Promotion repository — async database operations for the promotions table.
"""
from typing import Optional, List
from datetime import datetime

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.promotion import Promotion
from app.core.timezone import get_ist_now


async def get_active_promotions(db: AsyncSession) -> List[Promotion]:
    """
    Fetch all promotions that are:
      - active (is_active=True)
      - not soft-deleted
      - within their validity window (or have no window set)
    Ordered by sort_order ASC then created_at DESC.
    """
    now = get_ist_now()

    # valid_from condition: NULL or past
    valid_from_ok = or_(Promotion.valid_from.is_(None), Promotion.valid_from <= now)
    # valid_until condition: NULL or future
    valid_until_ok = or_(Promotion.valid_until.is_(None), Promotion.valid_until >= now)

    result = await db.execute(
        select(Promotion)
        .where(
            and_(
                Promotion.is_active == True,
                Promotion.deleted_at.is_(None),
                valid_from_ok,
                valid_until_ok,
            )
        )
        .order_by(Promotion.sort_order.asc(), Promotion.created_at.desc())
    )
    return list(result.scalars().all())


async def get_all_promotions_admin(db: AsyncSession) -> List[Promotion]:
    """Fetch all promotions for admin view (including inactive, excluding deleted)."""
    result = await db.execute(
        select(Promotion)
        .where(Promotion.deleted_at.is_(None))
        .order_by(Promotion.sort_order.asc(), Promotion.created_at.desc())
    )
    return list(result.scalars().all())


async def get_promotion_by_id(db: AsyncSession, promotion_id: int) -> Optional[Promotion]:
    result = await db.execute(
        select(Promotion).where(
            Promotion.id == promotion_id, Promotion.deleted_at.is_(None)
        )
    )
    return result.scalar_one_or_none()


async def create_promotion(db: AsyncSession, data: dict) -> Promotion:
    promo = Promotion(**data)
    db.add(promo)
    await db.commit()
    await db.refresh(promo)
    return promo


async def update_promotion(db: AsyncSession, promo: Promotion, data: dict) -> Promotion:
    for field, value in data.items():
        if value is not None or field in ("subtitle", "icon_emoji", "cta_label", "cta_url", "bg_gradient", "discount_value", "valid_from", "valid_until"):
            setattr(promo, field, value)
    db.add(promo)
    await db.commit()
    await db.refresh(promo)
    return promo


async def soft_delete_promotion(db: AsyncSession, promo: Promotion) -> None:
    promo.deleted_at = get_ist_now()
    db.add(promo)
    await db.commit()
