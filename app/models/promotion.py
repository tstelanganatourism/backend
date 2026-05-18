"""
Promotion model — Phase-3 Promotional Banner System.

Promotions are admin-controlled, database-backed offers that appear on the
public-facing scrolling banner. Coupon redemption / booking discount logic
is NOT implemented here (belongs in the transactional phase).
"""
from sqlalchemy import (
    Column, String, Boolean, DateTime, Numeric,
    Enum as SQLEnum, BigInteger,
)
from sqlalchemy import Index

from app.models.base import BaseModel, SortableMixin
from app.models.enums import PromotionType, PromotionTarget, PromotionBadge


class Promotion(BaseModel, SortableMixin):
    __tablename__ = "promotions"

    # Display content
    title          = Column(String(255), nullable=False)          # e.g. "₹500 OFF Godavari Cruise"
    subtitle       = Column(String(512), nullable=True)           # e.g. "Valid this weekend only"
    icon_emoji     = Column(String(8), nullable=True)             # e.g. "🛥️"
    badge          = Column(
                        SQLEnum(PromotionBadge, name="promotionbadge"),
                        nullable=False,
                        default=PromotionBadge.NONE,
                        server_default="NONE",
                    )

    # Type and targeting
    type           = Column(
                        SQLEnum(PromotionType, name="promotiontype"),
                        nullable=False,
                    )
    target         = Column(
                        SQLEnum(PromotionTarget, name="promotiontarget"),
                        nullable=False,
                        default=PromotionTarget.ALL,
                        server_default="ALL",
                    )

    # Discount value — NULL for INFORMATIONAL / CAMPAIGN types
    discount_value = Column(Numeric(10, 2), nullable=True)

    # CTA
    cta_label      = Column(String(64), nullable=True)            # e.g. "Book Now"
    cta_url        = Column(String(512), nullable=True)           # e.g. "/packages"

    # Visual
    bg_gradient    = Column(String(255), nullable=True)           # Tailwind gradient classes

    # Scheduling and visibility
    is_active      = Column(Boolean, nullable=False, default=True, server_default="true")
    valid_from     = Column(DateTime(timezone=True), nullable=True)
    valid_until    = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Fast lookup for the public banner query
        Index("ix_promotions_is_active", "is_active"),
        Index("ix_promotions_valid_from", "valid_from"),
        Index("ix_promotions_valid_until", "valid_until"),
        Index("ix_promotions_sort_order", "sort_order"),
    )
