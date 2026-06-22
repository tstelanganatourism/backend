from typing import Optional
from datetime import datetime, date
from pydantic import Field, field_validator
from decimal import Decimal
from app.schemas.base import AppBaseModel, TimestampSchema

class CouponBase(AppBaseModel):
    code: str = Field(..., min_length=3, max_length=32)
    discount_type: str = Field(..., description="FLAT or PERCENTAGE")
    discount_value: Decimal = Field(..., ge=0)
    min_booking_amount: Optional[Decimal] = Field(None, ge=0)
    max_discount_amount: Optional[Decimal] = Field(None, ge=0)
    min_tickets: Optional[int] = Field(None, ge=1)
    usage_limit: Optional[int] = Field(None, ge=1)
    applicable_package_ids: list[int] = Field(default_factory=list)
    applicable_room_ids: list[int] = Field(default_factory=list)
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    is_active: bool = True
    is_weekend_only: bool = False
    @field_validator("discount_type")
    @classmethod
    def validate_discount_type(cls, v: str) -> str:
        v_upper = v.upper()
        if v_upper not in ("FLAT", "PERCENTAGE"):
            raise ValueError("discount_type must be FLAT or PERCENTAGE")
        return v_upper

class CouponCreate(CouponBase):
    pass

class CouponUpdate(AppBaseModel):
    code: Optional[str] = Field(None, min_length=3, max_length=32)
    discount_type: Optional[str] = Field(None, description="FLAT or PERCENTAGE")
    discount_value: Optional[Decimal] = Field(None, ge=0)
    min_booking_amount: Optional[Decimal] = Field(None, ge=0)
    max_discount_amount: Optional[Decimal] = Field(None, ge=0)
    min_tickets: Optional[int] = Field(None, ge=1)
    usage_limit: Optional[int] = Field(None, ge=1)
    applicable_package_ids: Optional[list[int]] = None
    applicable_room_ids: Optional[list[int]] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    is_active: Optional[bool] = None
    is_weekend_only: Optional[bool] = None

    @field_validator("discount_type")
    @classmethod
    def validate_discount_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v_upper = v.upper()
        if v_upper not in ("FLAT", "PERCENTAGE"):
            raise ValueError("discount_type must be FLAT or PERCENTAGE")
        return v_upper

class CouponResponse(CouponBase, TimestampSchema):
    id: int
    usage_count: int

class CouponValidateRequest(AppBaseModel):
    code: str
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    booking_amount: float
    ticket_count: int = 0
    travel_date: Optional[date] = None

class CouponValidateResponse(AppBaseModel):
    valid: bool
    discount_amount: float = 0.0
    discounted_subtotal: float = 0.0
    reason: Optional[str] = None
