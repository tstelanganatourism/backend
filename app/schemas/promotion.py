"""
Promotion schemas — public display and admin CRUD.
"""
from typing import Optional
from datetime import datetime
from pydantic import Field
from app.schemas.base import AppBaseModel, TimestampSchema
from app.models.enums import PromotionType, PromotionTarget, PromotionBadge


# ─── Public ───────────────────────────────────────────────────────────────────

class PromotionPublicResponse(AppBaseModel):
    """Minimal schema shown to the public banner — no internal details."""
    id: int
    title: str
    subtitle: Optional[str] = None
    icon_emoji: Optional[str] = None
    badge: PromotionBadge
    type: PromotionType
    target: PromotionTarget
    discount_value: Optional[float] = None
    cta_label: Optional[str] = None
    cta_url: Optional[str] = None
    bg_gradient: Optional[str] = None
    sort_order: int = 0


# ─── Admin ────────────────────────────────────────────────────────────────────

class PromotionAdminCreate(AppBaseModel):
    title: str = Field(..., min_length=3, max_length=255)
    subtitle: Optional[str] = Field(None, max_length=512)
    icon_emoji: Optional[str] = Field(None, max_length=8)
    badge: PromotionBadge = PromotionBadge.NONE
    type: PromotionType
    target: PromotionTarget = PromotionTarget.ALL
    discount_value: Optional[float] = Field(None, ge=0)
    cta_label: Optional[str] = Field(None, max_length=64)
    cta_url: Optional[str] = Field(None, max_length=512)
    bg_gradient: Optional[str] = Field(None, max_length=255)
    is_active: bool = True
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    sort_order: int = 0


class PromotionAdminUpdate(AppBaseModel):
    title: Optional[str] = Field(None, min_length=3, max_length=255)
    subtitle: Optional[str] = Field(None, max_length=512)
    icon_emoji: Optional[str] = Field(None, max_length=8)
    badge: Optional[PromotionBadge] = None
    type: Optional[PromotionType] = None
    target: Optional[PromotionTarget] = None
    discount_value: Optional[float] = Field(None, ge=0)
    cta_label: Optional[str] = Field(None, max_length=64)
    cta_url: Optional[str] = Field(None, max_length=512)
    bg_gradient: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    sort_order: Optional[int] = None


class PromotionAdminResponse(PromotionAdminCreate, TimestampSchema):
    id: int
    is_active: bool
