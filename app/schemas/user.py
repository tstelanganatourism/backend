"""
Auth request/response schemas + User profile schemas — Phase-3.
"""
import re
from typing import Optional, Any
from pydantic import EmailStr, Field, field_validator, ConfigDict
from app.schemas.base import AppBaseModel, TimestampSchema
from app.models.enums import UserRole, AccountStatus

# ─── Shared Name Validation ──────────────────────────────────────────────────

_NAME_REGEX = re.compile(r"^[A-Za-z\s'\-\.]{2,100}$")
_DANGEROUS_PATTERNS = [
    "<script", "javascript:", "onerror=", "onload=",
    "iframe", "<img", "<", ">",
]

def _validate_human_name(v: str) -> str:
    """Validate a human name — rejects XSS payloads, allows real names."""
    if v is None:
        return v
    cleaned = v.strip()
    if not cleaned:
        raise ValueError("Name cannot be empty")
    lower = cleaned.lower()
    for pattern in _DANGEROUS_PATTERNS:
        if pattern in lower:
            raise ValueError("Name contains invalid characters")
    if not _NAME_REGEX.match(cleaned):
        raise ValueError("Name contains invalid characters")
    return cleaned


# ─── Base User ────────────────────────────────────────────────────────────────

class UserBase(AppBaseModel):
    email: EmailStr
    full_name: str
    phone_number: Optional[str] = None
    role: UserRole = UserRole.USER


class UserCreate(UserBase):
    password: str = Field(..., min_length=8)


class UserResponse(UserBase, TimestampSchema):
    id: int
    account_status: AccountStatus


# ─── Tourist Auth ─────────────────────────────────────────────────────────────

class TouristSignupRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(..., min_length=2, max_length=100)
    email: Optional[EmailStr] = None
    password: str = Field(..., min_length=8, max_length=128)
    phone_number: Optional[str] = Field(None, max_length=20)

    @field_validator("full_name", mode="before")
    @classmethod
    def validate_full_name(cls, v: str) -> str:
        return _validate_human_name(v)

    @field_validator("email", mode="before")
    @classmethod
    def strip_email(cls, v: Optional[str]) -> Optional[str]:
        if v:
            return v.strip() or None
        return None

    @field_validator("phone_number", mode="before")
    @classmethod
    def strip_phone(cls, v: Optional[str]) -> Optional[str]:
        if v:
            return v.strip() or None
        return None

    def model_post_init(self, __context: Any) -> None:
        if not self.email and not self.phone_number:
            raise ValueError("Please provide at least an email address or phone number.")


class TouristLoginRequest(AppBaseModel):
    """Accepts email or phone number as login_id, plus password."""
    login_id: str = Field(..., min_length=1, description="Email address or 10-digit phone number")
    password: str = Field(..., min_length=1)


# ─── Agent Auth ───────────────────────────────────────────────────────────────

class AgentLoginRequest(AppBaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


# ─── Admin Auth (2-step: password → OTP) ────────────────────────────────────

class AdminLoginRequest(AppBaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class AdminOTPVerifyRequest(AppBaseModel):
    user_id: int
    otp: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


# ─── Google OAuth ─────────────────────────────────────────────────────────────

class GoogleCallbackRequest(AppBaseModel):
    code: str
    redirect_uri: Optional[str] = None  # override for prod vs dev


# ─── Token Responses ─────────────────────────────────────────────────────────

class UserMeResponse(AppBaseModel):
    id: int
    email: Optional[str]
    full_name: str
    role: UserRole
    account_status: AccountStatus
    phone_number: Optional[str] = None
    avatar_url: Optional[str] = None
    commission_percentage: Optional[float] = None
    commission_type: Optional[str] = None
    commission_fixed_amount: Optional[float] = None
    company_name: Optional[str] = None
    gst_number: Optional[str] = None
    address: Optional[str] = None
    admin_notes: Optional[str] = None


class ProfileUpdateRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = None
    avatar_url: Optional[str] = None
    gst_number: Optional[str] = None
    address: Optional[str] = None

    @field_validator("full_name", mode="before")
    @classmethod
    def validate_full_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_human_name(v)

    @field_validator("email", mode="before")
    @classmethod
    def strip_email(cls, v: Optional[str]) -> Optional[str]:
        if v:
            return v.strip() or None
        return None


class TokenResponse(AppBaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserMeResponse


class OTPInitiatedResponse(AppBaseModel):
    """Returned after admin password validation. Step 1 of 2."""
    user_id: int
    message: str = "OTP sent to registered email. Valid for 5 minutes."


class RefreshResponse(AppBaseModel):
    access_token: str
    token_type: str = "bearer"


class ForgotPasswordRequest(AppBaseModel):
    """Accepts email OR phone number (login_id) to look up the account."""
    login_id: str = Field(..., min_length=1, description="Email address or 10-digit phone number")


class ResetPasswordRequest(AppBaseModel):
    login_id: str = Field(..., min_length=1, description="Email address or 10-digit phone number")
    otp: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(..., min_length=8, max_length=128)
