"""
Auth API router — Phase-3 full implementation.

Endpoints:
  Tourist:
    POST /tourist/signup
    POST /tourist/login
    GET  /google/url
    POST /google/callback

  Agent:
    POST /agent/login

  Admin (2-step):
    POST /admin/login      → send OTP
    POST /admin/verify-otp → verify OTP, return tokens

  Shared:
    POST /refresh
    POST /logout
    GET  /me
"""
from loguru import logger
from datetime import timedelta
from html import escape
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, UploadFile, File, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_otp,
    get_password_hash,
    verify_password,
)
from app.core.timezone import get_ist_now
from app.db.session import get_db
from app.middleware.auth import get_current_user
from app.models.enums import UserRole, AccountStatus
from app.models.user import User
from app.repositories.user_repository import (
    create_google_user,
    create_tourist_user,
    get_user_by_email,
    get_user_by_google_id,
    get_user_by_id,
    get_user_by_phone,
    link_google_to_existing,
    update_last_login,
)
from app.schemas.user import (
    AdminLoginRequest,
    AdminOTPVerifyRequest,
    AgentLoginRequest,
    GoogleCallbackRequest,
    OTPInitiatedResponse,
    RefreshResponse,
    TokenResponse,
    TouristLoginRequest,
    TouristSignupRequest,
    UserMeResponse,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    ProfileUpdateRequest,
    PhoneOTPSendRequest,
    PhoneOTPVerifyRequest,
    PhoneOTPSendResponse,
)
from app.services.redis_client import (
    blacklist_token,
    is_token_blacklisted,
    store_otp,
    verify_and_consume_otp,
    verify_otp_only,
    store_sms_otp,
    verify_and_consume_sms_otp,
    check_sms_otp_rate_limit,
    record_sms_otp_send,
)

router = APIRouter()

# ─── Cookie Configuration ────────────────────────────────────────────────────

REFRESH_COOKIE_NAME = "refresh_token"
COOKIE_SECURE = settings.ENVIRONMENT == "production"
COOKIE_SAMESITE = "lax"
AP_TOURISM_EMAIL_LOGO_URL = "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1779358705/b66b077a-69fa-4625-8b49-9a168efde88f.png"
TS_TOURISM_EMAIL_LOGO_URL = "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1779358643/22175967-f7df-420e-adcd-b4a37725fd5f.png"


from datetime import datetime, timezone, timedelta

def _set_refresh_cookie(response: Response, token: str, max_age_seconds: int) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=max_age_seconds,
        path="/",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path="/",
    )


def _build_token_response(user, response: Response, admin: bool = False) -> dict:
    """
    Create access + refresh tokens, set the HttpOnly cookie, return the dict
    suitable for a TokenResponse.
    """
    if admin:
        access_delta = timedelta(minutes=settings.ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES)
        refresh_delta = timedelta(hours=settings.ADMIN_REFRESH_TOKEN_EXPIRE_HOURS)
        refresh_max_age = settings.ADMIN_REFRESH_TOKEN_EXPIRE_HOURS * 3600
    else:
        access_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        refresh_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        refresh_max_age = settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400

    access_token, _ = create_access_token(user.id, user.role.value, expires_delta=access_delta)
    refresh_token, _ = create_refresh_token(user.id, user.role.value, expires_delta=refresh_delta)

    _set_refresh_cookie(response, refresh_token, max_age_seconds=refresh_max_age)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "account_status": user.account_status,
            "phone_number": user.phone_number,
            "avatar_url": user.avatar_url,
            "commission_percentage": float(user.commission_percentage) if user.commission_percentage is not None else None,
            "commission_type": user.commission_type,
            "commission_fixed_amount": float(user.commission_fixed_amount) if user.commission_fixed_amount is not None else None,
            "company_name": user.company_name,
            "gst_number": user.gst_number,
            "address": user.address,
            "admin_notes": user.admin_notes,
        },
    }


# ─── Send OTP email via Brevo ─────────────────────────────────────────────────

def _build_otp_email_html(
    *,
    full_name: str,
    otp: str,
    title: str,
    eyebrow: str,
    intro: str,
    expiry_text: str,
    security_note: str,
    accent_color: str = "#075b60",
) -> str:
    """Build a responsive, email-client-safe OTP email."""
    safe_name = escape(full_name or "there")
    safe_otp = escape(otp)
    safe_title = escape(title)
    safe_eyebrow = escape(eyebrow)
    safe_intro = escape(intro)
    safe_expiry_text = escape(expiry_text)
    safe_security_note = escape(security_note)
    safe_accent_color = escape(accent_color, quote=True)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta name="x-apple-disable-message-reformatting">
        <title>{safe_title}</title>
        <style>
            body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
            table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
            img {{ -ms-interpolation-mode: bicubic; border: 0; outline: none; text-decoration: none; }}
            body {{ margin: 0; padding: 0; background-color: #f6faf8; }}
            @media only screen and (max-width: 620px) {{
                .email-shell {{ width: 100% !important; }}
                .mobile-pad {{ padding-left: 20px !important; padding-right: 20px !important; }}
                .brand-title {{ font-size: 22px !important; line-height: 28px !important; }}
                .otp-code {{ font-size: 36px !important; letter-spacing: 8px !important; }}
                .logo-img {{ width: 64px !important; height: 64px !important; }}
            }}
        </style>
    </head>
    <body style="margin:0; padding:0; background-color:#f6faf8; font-family:'Helvetica Neue', Helvetica, Arial, sans-serif;">
        <div style="display:none; max-height:0; overflow:hidden; opacity:0; color:transparent;">
            Your TS Boat Tourism security code is {safe_otp}.
        </div>
        <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#f6faf8; margin:0; padding:0;">
            <tr>
                <td align="center" style="padding:40px 12px;">
                    <table role="presentation" class="email-shell" width="600" border="0" cellpadding="0" cellspacing="0" style="width:600px; max-width:600px; background-color:#ffffff; border:1px solid #e2e8f0; border-radius:24px; overflow:hidden; box-shadow: 0 4px 12px rgba(15, 47, 61, 0.03);">
                        <!-- Header -->
                        <tr>
                            <td align="center" class="mobile-pad" style="background-color:#0f2f3d; padding:32px 40px 32px 40px; border-bottom: 4px solid #075b60;">
                                <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin:0 auto 12px auto;">
                                    <tr>
                                        <td align="center">
                                            <img class="logo-img" src="https://res.cloudinary.com/r929tquv/image/upload/v1784575768/ts_tours/branding/logo.jpg" width="76" height="76" alt="TS Boat Tourism" style="display:block; width:76px; height:76px; border-radius:38px; border:2px solid #ffffff; background-color:#ffffff; object-fit:cover;">
                                        </td>
                                    </tr>
                                </table>
                                <div class="brand-title" style="color:#ffffff; font-size:24px; line-height:30px; font-weight:800; letter-spacing:-0.5px; margin:0 0 4px 0;">TS Boat Tourism</div>
                                <div style="color:#a5f3fc; font-size:12px; line-height:16px; font-weight:700; letter-spacing:1px; text-transform:uppercase;">{safe_eyebrow}</div>
                            </td>
                        </tr>
                        <!-- Content -->
                        <tr>
                            <td class="mobile-pad" style="padding:40px 48px 32px 48px; color:#1e293b;">
                                <div style="font-size:11px; line-height:16px; font-weight:800; letter-spacing:1.5px; text-transform:uppercase; color:{safe_accent_color}; margin:0 0 12px 0;">Verification Required</div>
                                <h1 style="margin:0 0 20px 0; color:#0f2f3d; font-size:24px; line-height:30px; font-weight:800; letter-spacing:-0.5px;">{safe_title}</h1>
                                <p style="margin:0 0 16px 0; color:#475569; font-size:15px; line-height:24px; font-weight:500;">Hello {safe_name},</p>
                                <p style="margin:0 0 28px 0; color:#475569; font-size:15px; line-height:24px; font-weight:400;">{safe_intro}</p>

                                <!-- OTP Box -->
                                <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="border:1px solid #ccfbf1; border-radius:20px; background-color:#f0fdfa; margin-bottom:28px;">
                                    <tr>
                                        <td align="center" style="padding:28px 24px;">
                                            <div style="color:#0f766e; font-size:12px; line-height:16px; font-weight:800; text-transform:uppercase; letter-spacing:1.5px; margin-bottom:12px;">Verification Code</div>
                                            <div class="otp-code" style="display:inline-block; background-color:#ffffff; border:2px solid #99f6e4; border-radius:16px; color:#0f2f3d; font-size:40px; line-height:50px; font-weight:900; letter-spacing:10px; padding:14px 24px 14px 34px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); font-family: Courier, monospace;">{safe_otp}</div>
                                        </td>
                                    </tr>
                                </table>

                                <!-- Expiry & Security Warning -->
                                <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
                                    <tr>
                                        <td style="background-color:#fffbeb; border:1px solid #fde68a; border-radius:16px; padding:16px 20px;">
                                            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
                                                <tr>
                                                    <td valign="top" style="padding-right:10px; font-size:16px; line-height:20px;">⚠️</td>
                                                    <td>
                                                        <div style="color:#b45309; font-size:13px; line-height:18px; font-weight:800; margin-bottom:2px;">{safe_expiry_text}</div>
                                                        <div style="color:#d97706; font-size:12px; line-height:17px; font-weight:500;">{safe_security_note}</div>
                                                    </td>
                                                </tr>
                                            </table>
                                        </td>
                                    </tr>
                                </table>
                            </td>
                        </tr>
                        <!-- Footer -->
                        <tr>
                            <td align="center" class="mobile-pad" style="background-color:#0f2f3d; border-top:1px solid #e2e8f0; padding:32px 40px; color:#94a3b8; font-size:12px; line-height:18px;">
                                <div style="color:#ffffff; font-size:14px; line-height:20px; font-weight:800; margin-bottom:6px;">TS Boat Tourism Portal</div>
                                <div style="margin-bottom:16px; font-weight:500;">Official Booking System for Bhadrachalam &amp; Papikondalu Cruises</div>
                                <div style="color:#64748b; font-size:11px; line-height:16px; border-top:1px solid #1e293b; padding-top:16px;">This is an automated security transmission. Please do not reply directly to this email.</div>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """


async def _send_password_reset_otp_email(email: str, full_name: str, otp: str):
    """Send a password reset OTP email via Brevo."""
    primary_key = settings.BREVO_API_KEY_ADMIN or settings.BREVO_API_KEY
    primary_from = settings.BREVO_FROM_EMAIL_ADMIN or settings.BREVO_FROM_EMAIL
    backup_key = settings.BREVO_API_KEY_BACKUP
    backup_from = settings.BREVO_FROM_EMAIL_BACKUP or settings.BREVO_FROM_EMAIL

    if not primary_key and not backup_key:
        logger.warning("No BREVO_API_KEY_ADMIN, BREVO_API_KEY, or BREVO_API_KEY_BACKUP configured, skipping reset email.")
        return False

    html_content = _build_otp_email_html(
        full_name=full_name,
        otp=otp,
        title="Password Reset Code",
        eyebrow="Account recovery",
        intro="We received a request to reset your TS Boating & Tourism password. Enter the code below to continue and create a new password.",
        expiry_text="This code expires in 10 minutes.",
        security_note="If you did not request a password reset, ignore this email and keep your current password unchanged.",
        accent_color="#b42318",
    )

    async def _attempt_send(api_key: str, from_email: str) -> tuple[bool, str]:
        payload = {
            "sender": {"email": from_email, "name": "TS Tourism"},
            "to": [{"email": email, "name": full_name}],
            "subject": "Password Reset Code - TS Tourism",
            "htmlContent": html_content,
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.brevo.com/v3/smtp/email",
                    json=payload,
                    headers={
                        "api-key": api_key,
                        "Content-Type": "application/json"
                    },
                    timeout=10.0,
                )
                if resp.status_code not in (200, 201):
                    return False, f"Brevo API Error: {resp.status_code} - {resp.text}"
                return True, ""
        except Exception as e:
            return False, f"Exception: {str(e)}"

    # 1. Try Admin (Secondary) key
    if primary_key:
        success, error_msg = await _attempt_send(primary_key, primary_from)
        if success:
            logger.info(f"Password reset email sent to {email} successfully using primary admin key.")
            return True
        logger.warning(f"Primary Admin Brevo key failed for Reset OTP to {email}: {error_msg}. Attempting Backup key...")

    # 2. Try Backup key
    if backup_key:
        success, error_msg = await _attempt_send(backup_key, backup_from)
        if success:
            logger.info(f"Password reset email sent to {email} successfully using backup key.")
            return True
        logger.error(f"Backup Brevo key also failed for Reset OTP to {email}: {error_msg}")

    return False


async def _send_admin_otp_email(email: str, full_name: str, otp: str):
    """Send an OTP email via Brevo."""
    if settings.ENVIRONMENT == "development":
        logger.info(f"===========================================================")
        logger.info(f"ADMIN LOGIN OTP FOR {email}: {otp}")
        logger.info(f"===========================================================")
        
        # Write to current_otp.txt in development mode for programmatic verification
        try:
            with open("current_otp.txt", "w") as f:
                f.write(otp)
        except Exception as e:
            logger.error(f"Failed to write OTP to current_otp.txt: {str(e)}")

    primary_key = settings.BREVO_API_KEY_ADMIN or settings.BREVO_API_KEY
    primary_from = settings.BREVO_FROM_EMAIL_ADMIN or settings.BREVO_FROM_EMAIL
    backup_key = settings.BREVO_API_KEY_BACKUP
    backup_from = settings.BREVO_FROM_EMAIL_BACKUP or settings.BREVO_FROM_EMAIL

    if not primary_key and not backup_key:
        logger.warning("No BREVO_API_KEY_ADMIN, BREVO_API_KEY, or BREVO_API_KEY_BACKUP configured, skipping actual email send.")
        return

    html_content = _build_otp_email_html(
        full_name=full_name,
        otp=otp,
        title="Admin Verification Code",
        eyebrow="Secure admin portal",
        intro="Use this one-time security code to finish signing in to the TS Tourism Admin Portal.",
        expiry_text="This code expires in 5 minutes.",
        security_note="If you did not try to sign in, ignore this email and review access to your admin account.",
        accent_color="#075b60",
    )

    async def _attempt_send(api_key: str, from_email: str) -> tuple[bool, str]:
        payload = {
            "sender": {"email": from_email, "name": "TS Tourism Services"},
            "to": [{"email": email, "name": full_name}],
            "subject": "Verification Code - TS Tourism Admin",
            "htmlContent": html_content,
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.brevo.com/v3/smtp/email",
                    json=payload,
                    headers={
                        "api-key": api_key,
                        "Content-Type": "application/json"
                    },
                    timeout=10.0,
                )
                if resp.status_code not in (200, 201):
                    return False, f"Brevo API Error: {resp.status_code} - {resp.text}"
                return True, ""
        except Exception as e:
            return False, f"Exception: {str(e)}"

    # 1. Try Admin (Secondary) key
    if primary_key:
        success, error_msg = await _attempt_send(primary_key, primary_from)
        if success:
            logger.info(f"Admin OTP email sent to {email} successfully using primary admin key.")
            return True
        logger.warning(f"Primary Admin Brevo key failed for OTP to {email}: {error_msg}. Attempting Backup key...")

    # 2. Try Backup key
    if backup_key:
        success, error_msg = await _attempt_send(backup_key, backup_from)
        if success:
            logger.info(f"Admin OTP email sent to {email} successfully using backup key.")
            return True
        logger.error(f"Backup Brevo key also failed for OTP to {email}: {error_msg}")

    return False


@router.post("/admin/resend-otp")
async def admin_resend_otp(
    body: AdminLoginRequest, # Reuse email/password for security context
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Resend a fresh OTP to the admin if requested."""
    user = await get_user_by_email(db, body.email)
    if not user or user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid request.")
    
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid request.")

    # Generate new OTP
    otp = generate_otp()
    await store_otp(user.id, otp)
    
    # Send email
    background_tasks.add_task(_send_admin_otp_email, user.email, user.full_name, otp)
    
    return {"message": "OTP resent successfully."}


# ═══════════════════════════════════════════════════════════════════════════════
# PHONE OTP LOGIN (Tourist only — agent/admin pages are separate and untouched)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/otp/send", response_model=PhoneOTPSendResponse)
async def phone_otp_send(
    body: PhoneOTPSendRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Send a 6-digit SMS OTP to a phone number for tourist login.
    Rate limits: 60s cooldown, max 3 sends per 10 minutes.
    Works for all roles — finds existing user or will create tourist on /otp/verify.
    """
    from app.services.sms_service import send_otp_sms

    rate = await check_sms_otp_rate_limit(body.phone)
    if not rate["allowed"]:
        if rate["locked"]:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many attempts. Please wait {rate['cooldown_seconds']} seconds before trying again.",
            )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {rate['cooldown_seconds']} seconds before requesting a new OTP.",
        )

    # Check if account is blocked (only if user exists)
    existing_user = await get_user_by_phone(db, body.phone)
    if existing_user and existing_user.account_status == AccountStatus.BLOCKED:
        raise HTTPException(status_code=403, detail="Your account has been suspended.")

    otp = generate_otp()
    await store_sms_otp(body.phone, otp)
    await record_sms_otp_send(body.phone)

    # Send SMS (fire-and-forget — don’t fail the request if SMS provider is slow)
    try:
        await send_otp_sms(body.phone, otp)
    except Exception as e:
        logger.error(f"OTP SMS send error for {body.phone}: {e}")

    if settings.ENVIRONMENT == "development":
        logger.info(f"===== DEV OTP for {body.phone}: {otp} =====")

    new_rate = await check_sms_otp_rate_limit(body.phone)
    return PhoneOTPSendResponse(
        message="OTP sent successfully. Valid for 5 minutes.",
        cooldown_seconds=60,
        attempts_remaining=new_rate["attempts_remaining"],
    )


@router.post("/otp/verify", response_model=TokenResponse)
async def phone_otp_verify(
    body: PhoneOTPVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Verify the SMS OTP and log in. Auto-creates tourist account if phone is new.
    Returns JWT tokens and role so frontend can redirect correctly.
    """
    from app.repositories.user_repository import get_or_create_tourist_by_phone

    valid = await verify_and_consume_sms_otp(body.phone, body.otp)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired OTP. Please request a new one.",
        )

    user = await get_or_create_tourist_by_phone(db, body.phone)

    if user.account_status == AccountStatus.BLOCKED:
        raise HTTPException(status_code=403, detail="Your account has been suspended.")

    is_admin = user.role == UserRole.ADMIN
    await update_last_login(db, user)
    return _build_token_response(user, response, admin=is_admin)


# ═══════════════════════════════════════════════════════════════════════════════
# TOURIST ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/tourist/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def tourist_signup(
    body: TouristSignupRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Register a new tourist account and return tokens immediately."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Password registration is disabled for tourists. Please register and verify using Phone OTP."
    )


@router.post("/tourist/login", response_model=TokenResponse)
async def tourist_login(
    body: TouristLoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate a tourist with email or phone number + password."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Password authentication is disabled for tourists. Please log in using Phone OTP."
    )


@router.post("/forgot-password")
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db)
):
    """Initiate password reset flow. Looks up account by email or phone."""
    login_id = body.login_id.strip()

    # Resolve user by email or phone
    if login_id.isdigit() and len(login_id) == 10:
        user = await get_user_by_phone(db, login_id)
    else:
        user = await get_user_by_email(db, login_id)
        if not user and login_id.isdigit():
            user = await get_user_by_phone(db, login_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email or phone number."
        )

    if user.account_status == AccountStatus.BLOCKED:
        raise HTTPException(status_code=403, detail="Account is suspended.")

    # If user has no email, we cannot send OTP — return special error code
    if not user.email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no_email_on_account"
        )

    # Generate and store OTP (10 min expiry)
    otp = generate_otp()
    await store_otp(user.id, otp, expire_seconds=600)
    if settings.ENVIRONMENT == "development":
        logger.info(f"Generated OTP {otp} for user {user.email}")

    # Send email
    success = await _send_password_reset_otp_email(user.email, user.full_name, otp)
    if not success:
        logger.error(f"Failed to send password reset email to {user.email}")

    return {"message": "If an account exists, a reset code has been sent.", "email": user.email}


@router.post("/verify-reset-otp")
async def verify_reset_otp(
    body: ForgotPasswordRequest,
    otp: str,
    db: AsyncSession = Depends(get_db)
):
    """Verify if the reset OTP is valid without consuming it yet."""
    login_id = body.login_id.strip()
    if login_id.isdigit() and len(login_id) == 10:
        user = await get_user_by_phone(db, login_id)
    else:
        user = await get_user_by_email(db, login_id)
        if not user and login_id.isdigit():
            user = await get_user_by_phone(db, login_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    valid = await verify_otp_only(user.id, otp)
    if not valid:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP.")

    return {"message": "OTP is valid."}


@router.post("/reset-password")
async def reset_password(
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db)
):
    """Verify OTP and update password."""
    login_id = body.login_id.strip()
    if login_id.isdigit() and len(login_id) == 10:
        user = await get_user_by_phone(db, login_id)
    else:
        user = await get_user_by_email(db, login_id)
        if not user and login_id.isdigit():
            user = await get_user_by_phone(db, login_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    valid = await verify_and_consume_otp(user.id, body.otp)
    if not valid:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP.")

    from app.repositories.user_repository import update_user_password
    await update_user_password(db, user, body.new_password)

    return {"message": "Password updated successfully. Please login."}


# ═══════════════════════════════════════════════════════════════════════════════
# GOOGLE OAUTH (Tourist only)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/google/url")
async def google_auth_url(redirect_uri: Optional[str] = None, state: Optional[str] = None):
    """Disabled: Google OAuth is disabled."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Google authentication is disabled. Please log in using Phone OTP."
    )


@router.post("/google/callback", response_model=TokenResponse)
async def google_callback(
    body: GoogleCallbackRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Disabled: Google OAuth is disabled."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Google authentication is disabled. Please log in using Phone OTP."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/agent/login", response_model=TokenResponse)
async def agent_login(
    body: AgentLoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate an agent with admin-issued credentials (email + password)."""
    user = await get_user_by_email(db, body.email)
    invalid_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials.",
    )

    if not user:
        raise invalid_exc
    if user.role not in (UserRole.AGENT, UserRole.ADMIN):
        raise HTTPException(status_code=403, detail="Use the tourist portal for tourist accounts.")
    if not user.password_hash or not verify_password(body.password, user.password_hash):
        raise invalid_exc
    if user.account_status == AccountStatus.BLOCKED:
        raise HTTPException(status_code=403, detail="Your account has been suspended.")

    await update_last_login(db, user)
    return _build_token_response(user, response)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS (2-step: password + Redis OTP)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/admin/login", response_model=OTPInitiatedResponse)
async def admin_login(
    body: AdminLoginRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 1 of admin login.
    Verify admin credentials → generate OTP → send via Resend email.
    Returns user_id for the OTP verification step.
    """
    user = await get_user_by_email(db, body.email)
    invalid_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials.",
    )

    if not user:
        raise invalid_exc
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin portal is restricted to administrators.")
    if not user.password_hash or not verify_password(body.password, user.password_hash):
        raise invalid_exc
    if user.account_status == AccountStatus.BLOCKED:
        raise HTTPException(status_code=403, detail="Your account has been suspended.")

    # Generate and store OTP
    otp = generate_otp()
    await store_otp(user.id, otp)

    # Send OTP via Resend email
    background_tasks.add_task(_send_admin_otp_email, user.email, user.full_name, otp)

    return OTPInitiatedResponse(user_id=user.id)


@router.post("/admin/verify-otp", response_model=TokenResponse)
async def admin_verify_otp(
    body: AdminOTPVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Step 2 of admin login.
    Verify the 6-digit OTP from Redis and issue tokens.
    """
    user = await get_user_by_id(db, body.user_id)
    if not user or user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Invalid request.")

    valid = await verify_and_consume_otp(body.user_id, body.otp)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired OTP. Please restart the login process.",
        )

    await update_last_login(db, user)
    return _build_token_response(user, response, admin=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/refresh", response_model=TokenResponse)
async def refresh_access_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Rotate refresh token.
    Reads the HttpOnly refresh token cookie, validates it,
    blacklists the old refresh JTI, and issues a new access token.
    """
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token found. Please log in again.",
        )

    payload = decode_token(refresh_token, expected_type="refresh")

    jti: str = payload.get("jti", "")
    if jti and await is_token_blacklisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked. Please log in again.",
        )

    user_id: int = int(payload["sub"])
    user = await get_user_by_id(db, user_id)
    if not user or user.account_status in (AccountStatus.BLOCKED, AccountStatus.DISABLED):
        raise HTTPException(status_code=401, detail="Account is no longer active.")

    # Blacklist old refresh JTI (token rotation)
    from jose import jwt as _jwt
    from app.core.security import ALGORITHM
    old_payload = _jwt.decode(refresh_token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    old_exp = old_payload.get("exp", 0)
    old_jti = old_payload.get("jti", "")
    remaining = max(0, int(old_exp - get_ist_now().timestamp()))
    # NOTE: Immediate blacklisting causes random logouts if the user has multiple tabs open
    # and they both try to refresh the token at the exact same time. We will rely on 
    # the token's natural expiration instead, or implement a 60-second grace period later if needed.
    # if old_jti and remaining > 0:
    #     await blacklist_token(old_jti, remaining)

    # Issue new access token and new refresh token (Proper Token Rotation)
    is_admin = user.role == UserRole.ADMIN
    access_delta = timedelta(
        minutes=settings.ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES if is_admin
        else settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    refresh_delta = timedelta(
        hours=settings.ADMIN_REFRESH_TOKEN_EXPIRE_HOURS
    ) if is_admin else timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    refresh_max_age = (settings.ADMIN_REFRESH_TOKEN_EXPIRE_HOURS * 3600) if is_admin else (settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400)

    new_access_token, _ = create_access_token(user.id, user.role.value, expires_delta=access_delta)
    new_refresh_token, _ = create_refresh_token(user.id, user.role.value, expires_delta=refresh_delta)

    # Set the new refresh token in the cookie
    _set_refresh_cookie(response, new_refresh_token, max_age_seconds=refresh_max_age)

    return {
        "access_token": new_access_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "account_status": user.account_status,
            "phone_number": user.phone_number,
            "avatar_url": user.avatar_url,
            "commission_percentage": float(user.commission_percentage) if user.commission_percentage is not None else None,
            "commission_type": user.commission_type,
            "commission_fixed_amount": float(user.commission_fixed_amount) if user.commission_fixed_amount is not None else None,
            "company_name": user.company_name,
            "gst_number": user.gst_number,
            "address": user.address,
            "admin_notes": user.admin_notes,
        },
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Logout: blacklist the refresh token JTI and clear the cookie.
    The client is responsible for discarding the access token from memory.
    """
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)

    if refresh_token:
        try:
            from jose import jwt as _jwt
            from app.core.security import ALGORITHM
            payload = _jwt.decode(refresh_token, settings.SECRET_KEY, algorithms=[ALGORITHM])
            jti = payload.get("jti", "")
            exp = payload.get("exp", 0)
            remaining = max(0, int(exp - get_ist_now().timestamp()))
            if jti and remaining > 0:
                await blacklist_token(jti, remaining, is_logout=True)
        except Exception:
            pass  # Expired or tampered — safe to ignore, just clear the cookie

    _clear_refresh_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserMeResponse)
async def get_me(current_user=Depends(get_current_user)):
    """Return the authenticated user's profile."""
    return UserMeResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        account_status=current_user.account_status,
        phone_number=current_user.phone_number,
        avatar_url=current_user.avatar_url,
        commission_percentage=float(current_user.commission_percentage) if current_user.commission_percentage is not None else None,
        commission_type=current_user.commission_type,
        commission_fixed_amount=float(current_user.commission_fixed_amount) if current_user.commission_fixed_amount is not None else None,
        company_name=current_user.company_name,
        gst_number=current_user.gst_number,
        address=current_user.address,
        admin_notes=current_user.admin_notes,
    )


@router.put("/me", response_model=UserMeResponse)
async def update_me(
    body: ProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Update the authenticated user's profile."""
    import bleach
    if body.full_name is not None:
        current_user.full_name = bleach.clean(body.full_name, tags=[], strip=True).strip()
    if body.email is not None:
        # Only allow setting email if user doesn't already have one (phone-only accounts)
        if not current_user.email:
            existing = await get_user_by_email(db, str(body.email))
            if existing and existing.id != current_user.id:
                raise HTTPException(status_code=409, detail="This email is already used by another account.")
            current_user.email = str(body.email)
        # If user already has an email, silently ignore (can't change email)
    if body.phone_number is not None:
        if body.phone_number:
            import re
            cleaned_phone = re.sub(r"\D", "", body.phone_number)
            if len(cleaned_phone) != 10:
                raise HTTPException(status_code=400, detail="Invalid phone number. Must be a 10-digit mobile number.")
            
            existing = await get_user_by_phone(db, cleaned_phone)
            if existing and existing.id != current_user.id:
                raise HTTPException(status_code=409, detail="This phone number is already registered with another account.")
            current_user.phone_number = cleaned_phone
        else:
            current_user.phone_number = None
    if body.avatar_url is not None:
        current_user.avatar_url = body.avatar_url
    if body.gst_number is not None:
        current_user.gst_number = bleach.clean(body.gst_number, tags=[], strip=True).strip() if body.gst_number else None
    if body.address is not None:
        current_user.address = bleach.clean(body.address, tags=[], strip=True).strip() if body.address else None

    await db.commit()
    await db.refresh(current_user)

    return UserMeResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        account_status=current_user.account_status,
        phone_number=current_user.phone_number,
        avatar_url=current_user.avatar_url,
        commission_percentage=float(current_user.commission_percentage) if current_user.commission_percentage is not None else None,
        commission_type=current_user.commission_type,
        commission_fixed_amount=float(current_user.commission_fixed_amount) if current_user.commission_fixed_amount is not None else None,
        company_name=current_user.company_name,
        gst_number=current_user.gst_number,
        address=current_user.address,
        admin_notes=current_user.admin_notes,
    )


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Secure avatar upload for logged in users.
    Uploads to Cloudinary or saves locally to /static/uploads/ with 100% reliability.
    """
    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/webp", "image/gif"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image files (JPG, PNG, WEBP, GIF) are allowed."
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty image file.")

    avatar_url = None

    # 1. Try Cloudinary if configured
    if settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET:
        try:
            import cloudinary
            import cloudinary.uploader
            cloudinary.config(
                cloud_name=settings.CLOUDINARY_CLOUD_NAME,
                api_key=settings.CLOUDINARY_API_KEY,
                api_secret=settings.CLOUDINARY_API_SECRET,
                secure=True
            )
            upload_result = cloudinary.uploader.upload(
                contents,
                folder="ts_tours/avatars",
                resource_type="image"
            )
            avatar_url = upload_result.get("secure_url")
        except Exception as err:
            logger.warning(f"Cloudinary upload failed, using local static storage fallback: {err}")

    # 2. Local Static Storage Fallback if Cloudinary is not used or failed
    if not avatar_url:
        import os
        import uuid
        ext = ".png"
        if file.content_type == "image/jpeg":
            ext = ".jpg"
        elif file.content_type == "image/webp":
            ext = ".webp"
        elif file.content_type == "image/gif":
            ext = ".gif"

        filename = f"avatar_{current_user.id}_{uuid.uuid4().hex[:8]}{ext}"
        static_uploads_dir = os.path.join(os.path.dirname(__file__), "..", "..", "static", "uploads")
        os.makedirs(static_uploads_dir, exist_ok=True)
        file_path = os.path.join(static_uploads_dir, filename)

        with open(file_path, "wb") as f:
            f.write(contents)

        avatar_url = f"http://localhost:8000/static/uploads/{filename}"

    # Update DB
    current_user.avatar_url = avatar_url
    await db.commit()
    await db.refresh(current_user)

    return {
        "url": avatar_url,
        "public_id": None,
    }
