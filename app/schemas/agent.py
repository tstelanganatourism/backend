"""
Agent Pydantic schemas for admin CRUD operations.
"""
from typing import Optional
from datetime import datetime
from decimal import Decimal
from pydantic import Field, field_validator, EmailStr
from app.schemas.base import AppBaseModel, TimestampSchema


class AgentCreate(AppBaseModel):
    """Schema for creating a new agent."""
    full_name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    phone_number: str = Field(..., min_length=10, max_length=15)
    password: str = Field(..., min_length=6, max_length=128)
    commission_type: str = Field(default="PERCENTAGE")  # PERCENTAGE or FIXED_AMOUNT
    commission_percentage: Decimal = Field(default=Decimal("0.00"), ge=0, le=100)
    commission_fixed_amount: Optional[Decimal] = Field(None, ge=0)
    company_name: Optional[str] = Field(None, max_length=200)
    gst_number: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = None
    admin_notes: Optional[str] = None

    @field_validator("commission_type")
    @classmethod
    def validate_commission_type(cls, v: str) -> str:
        v_upper = v.upper()
        if v_upper not in ("PERCENTAGE", "FIXED_AMOUNT"):
            raise ValueError("commission_type must be PERCENTAGE or FIXED_AMOUNT")
        return v_upper

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        cleaned = v.strip().replace(" ", "").replace("-", "")
        if not cleaned.replace("+", "").isdigit():
            raise ValueError("Phone number must contain only digits and optional + prefix")
        return cleaned


class AgentUpdate(AppBaseModel):
    """Schema for updating an existing agent."""
    full_name: Optional[str] = Field(None, min_length=2, max_length=120)
    phone_number: Optional[str] = Field(None, min_length=10, max_length=15)
    commission_type: Optional[str] = None  # PERCENTAGE or FIXED_AMOUNT
    commission_percentage: Optional[Decimal] = Field(None, ge=0, le=100)
    commission_fixed_amount: Optional[Decimal] = Field(None, ge=0)
    company_name: Optional[str] = Field(None, max_length=200)
    gst_number: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = None
    admin_notes: Optional[str] = None
    account_status: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("commission_type")
    @classmethod
    def validate_commission_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v_upper = v.upper()
        if v_upper not in ("PERCENTAGE", "FIXED_AMOUNT"):
            raise ValueError("commission_type must be PERCENTAGE or FIXED_AMOUNT")
        return v_upper

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip().replace(" ", "").replace("-", "")
        if not cleaned.replace("+", "").isdigit():
            raise ValueError("Phone number must contain only digits and optional + prefix")
        return cleaned

    @field_validator("account_status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v_upper = v.upper()
        if v_upper not in ("ACTIVE", "BLOCKED", "DISABLED"):
            raise ValueError("account_status must be ACTIVE, BLOCKED, or DISABLED")
        return v_upper


class AgentResetPassword(AppBaseModel):
    """Schema for admin-initiated password reset."""
    new_password: str = Field(..., min_length=6, max_length=128)


class AgentResponse(AppBaseModel):
    """Agent list/detail response."""
    id: int
    full_name: str
    email: Optional[str] = None
    phone_number: Optional[str] = None
    role: str
    account_status: str
    is_active: bool
    commission_type: str = "PERCENTAGE"
    commission_percentage: Optional[Decimal] = None
    commission_fixed_amount: Optional[Decimal] = None
    company_name: Optional[str] = None
    gst_number: Optional[str] = None
    address: Optional[str] = None
    admin_notes: Optional[str] = None
    last_login: Optional[datetime] = None
    total_bookings: int = 0
    created_at: datetime
    updated_at: datetime


class AgentBookingMetrics(AppBaseModel):
    """Aggregated booking metrics for an agent."""
    total_bookings: int = 0
    confirmed_bookings: int = 0
    cancelled_bookings: int = 0
    pending_bookings: int = 0
    total_revenue: float = 0.0
    total_commission: float = 0.0


class AgentDetailResponse(AgentResponse):
    """Extended agent detail with booking metrics."""
    metrics: AgentBookingMetrics = AgentBookingMetrics()


class AgentPaginatedResponse(AppBaseModel):
    """Paginated response for agent listing."""
    items: list[AgentResponse]
    total: int
    page: int
    size: int


class AgentQuotaResponse(AppBaseModel):
    package_id: int
    package_title: str
    daily_quota: int
    is_allowed: bool


class AgentQuotaUpdate(AppBaseModel):
    package_id: int
    daily_quota: int = Field(default=10, ge=0)
    is_allowed: bool = Field(default=True)

