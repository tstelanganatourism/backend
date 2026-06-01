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
)
from app.services.redis_client import (
    blacklist_token,
    is_token_blacklisted,
    store_otp,
    verify_and_consume_otp,
    verify_otp_only,
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
    safe_logo1_url = escape(AP_TOURISM_EMAIL_LOGO_URL, quote=True)
    safe_logo2_url = escape(TS_TOURISM_EMAIL_LOGO_URL, quote=True)
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
            @media only screen and (max-width: 620px) {{
                .email-shell {{ width: 100% !important; }}
                .mobile-pad {{ padding-left: 20px !important; padding-right: 20px !important; }}
                .brand-title {{ font-size: 24px !important; line-height: 30px !important; }}
                .otp-code {{ font-size: 34px !important; letter-spacing: 8px !important; }}
                .logo-img {{ width: 74px !important; height: 74px !important; }}
            }}
        </style>
    </head>
    <body style="margin:0; padding:0; background-color:#eef3f6;">
        <div style="display:none; max-height:0; overflow:hidden; opacity:0; color:transparent;">
            Your TS Boating &amp; Tourism security code is {safe_otp}. It expires soon.
        </div>
        <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#eef3f6; margin:0; padding:0;">
            <tr>
                <td align="center" style="padding:28px 12px;">
                    <table role="presentation" class="email-shell" width="640" border="0" cellpadding="0" cellspacing="0" style="width:640px; max-width:640px; background-color:#ffffff; border:1px solid #dbe6ea; border-radius:18px; overflow:hidden;">
                        <tr>
                            <td align="center" class="mobile-pad" style="background-color:#075b60; padding:30px 34px 28px 34px;">
                                <table role="presentation" width="190" border="0" cellpadding="0" cellspacing="0" style="width:190px; margin:0 auto 16px auto;">
                                    <tr>
                                        <td align="center" width="95" style="padding:0 7px;">
                                            <img class="logo-img" src="{safe_logo1_url}" width="82" height="82" alt="APTDC" style="display:block; width:82px; height:82px; border-radius:41px;">
                                        </td>
                                        <td align="center" width="95" style="padding:0 7px;">
                                            <img class="logo-img" src="{safe_logo2_url}" width="82" height="82" alt="Telangana Tourism" style="display:block; width:82px; height:82px; border-radius:41px;">
                                        </td>
                                    </tr>
                                </table>
                                <div class="brand-title" style="font-family:Arial, Helvetica, sans-serif; color:#ffffff; font-size:28px; line-height:34px; font-weight:800; letter-spacing:0; margin:0 0 6px 0;">TS Boating &amp; Tourism</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#d6f4ef; font-size:14px; line-height:20px; font-weight:800;">{safe_eyebrow}</div>
                            </td>
                        </tr>
                        <tr>
                            <td class="mobile-pad" style="padding:38px 42px 30px 42px; font-family:Arial, Helvetica, sans-serif; color:#14313a;">
                                <div style="font-size:12px; line-height:17px; font-weight:800; letter-spacing:0.8px; text-transform:uppercase; color:{safe_accent_color}; margin:0 0 10px 0;">Secure verification</div>
                                <h1 style="margin:0 0 16px 0; color:#102f3a; font-size:25px; line-height:32px; font-weight:800; letter-spacing:0;">{safe_title}</h1>
                                <p style="margin:0 0 14px 0; color:#415865; font-size:15px; line-height:24px;">Hello {safe_name},</p>
                                <p style="margin:0 0 26px 0; color:#415865; font-size:15px; line-height:24px;">{safe_intro}</p>

                                <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="border:1px solid #dbe6ea; border-radius:16px; background-color:#f8fbfc;">
                                    <tr>
                                        <td align="center" style="padding:24px 18px 10px 18px;">
                                            <div style="font-family:Arial, Helvetica, sans-serif; color:#607380; font-size:11px; line-height:16px; font-weight:800; text-transform:uppercase; letter-spacing:1px;">One-time code</div>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td align="center" style="padding:0 18px 24px 18px;">
                                            <div class="otp-code" style="display:inline-block; background-color:#ffffff; border:1px solid #cfe0e5; border-radius:12px; color:{safe_accent_color}; font-family:Arial, Helvetica, sans-serif; font-size:42px; line-height:52px; font-weight:900; letter-spacing:12px; padding:12px 20px 12px 30px;">{safe_otp}</div>
                                        </td>
                                    </tr>
                                </table>

                                <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="margin:22px 0 0 0;">
                                    <tr>
                                        <td style="background-color:#fff8e7; border:1px solid #f1d89a; border-radius:12px; padding:14px 16px; font-family:Arial, Helvetica, sans-serif;">
                                            <div style="color:#7a4c00; font-size:13px; line-height:20px; font-weight:800;">{safe_expiry_text}</div>
                                            <div style="color:#8b6b2d; font-size:12px; line-height:18px; margin-top:4px;">{safe_security_note}</div>
                                        </td>
                                    </tr>
                                </table>
                            </td>
                        </tr>
                        <tr>
                            <td align="center" class="mobile-pad" style="background-color:#f7fafb; border-top:1px solid #dbe6ea; padding:24px 34px 28px 34px; font-family:Arial, Helvetica, sans-serif;">
                                <div style="color:#102f3a; font-size:14px; line-height:21px; font-weight:800; margin-bottom:6px;">TS Tourism Services</div>
                                <div style="color:#526a76; font-size:12px; line-height:18px;">This security email was sent to protect your account. Never share this code with anyone.</div>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>s
    </body>
    </html>
    """


async def _send_password_reset_otp_email(email: str, full_name: str, otp: str):
    """Send a password reset OTP email via Brevo."""
    if not settings.BREVO_API_KEY:
        return

    payload = {
        "sender": {"email": settings.BREVO_FROM_EMAIL, "name": "TS Tourism"},
        "to": [{"email": email, "name": full_name}],
        "subject": "Password Reset Code - TS Tourism",
        "htmlContent": _build_otp_email_html(
            full_name=full_name,
            otp=otp,
            title="Password Reset Code",
            eyebrow="Account recovery",
            intro="We received a request to reset your TS Boating & Tourism password. Enter the code below to continue and create a new password.",
            expiry_text="This code expires in 10 minutes.",
            security_note="If you did not request a password reset, ignore this email and keep your current password unchanged.",
            accent_color="#b42318",
        ),
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={
                    "api-key": settings.BREVO_API_KEY,
                    "Content-Type": "application/json"
                },
                timeout=10.0,
            )
            if resp.status_code not in (200, 201):
                logger.error(f"Brevo API Error (Password Reset): {resp.status_code} - {resp.text}")
                return False
            logger.info(f"Password reset email sent to {email} successfully.")
            return True
    except Exception as e:
        logger.error(f"Failed to send password reset email: {str(e)}")
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

    if not settings.BREVO_API_KEY:
        logger.warning("No BREVO_API_KEY found, skipping actual email send.")
        return

    payload = {
        "sender": {"email": settings.BREVO_FROM_EMAIL, "name": "TS Tourism Services"},
        "to": [{"email": email, "name": full_name}],
        "subject": "Verification Code - TS Tourism Admin",
        "htmlContent": _build_otp_email_html(
            full_name=full_name,
            otp=otp,
            title="Admin Verification Code",
            eyebrow="Secure admin portal",
            intro="Use this one-time security code to finish signing in to the TS Tourism Admin Portal.",
            expiry_text="This code expires in 5 minutes.",
            security_note="If you did not try to sign in, ignore this email and review access to your admin account.",
            accent_color="#075b60",
        ),
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={
                    "api-key": settings.BREVO_API_KEY,
                    "Content-Type": "application/json"
                },
                timeout=10.0,
            )
            if resp.status_code not in (200, 201):
                logger.error(f"Brevo API Error: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send OTP email: {str(e)}")


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
# TOURIST ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/tourist/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def tourist_signup(
    body: TouristSignupRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Register a new tourist account and return tokens immediately."""
    existing = await get_user_by_email(db, body.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user = await create_tourist_user(
        db,
        full_name=body.full_name,
        email=body.email,
        password=body.password,
        phone_number=body.phone_number,
    )

    return _build_token_response(user, response)


@router.post("/tourist/login", response_model=TokenResponse)
async def tourist_login(
    body: TouristLoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate a tourist with email + password."""
    user = await get_user_by_email(db, body.email)
    invalid_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password.",
    )

    if not user:
        raise invalid_exc
    if user.role != UserRole.USER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Use the correct login portal for your role.")
    if not user.password_hash or not verify_password(body.password, user.password_hash):
        raise invalid_exc
    if user.account_status == AccountStatus.BLOCKED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your account has been suspended.")

    await update_last_login(db, user)
    return _build_token_response(user, response)


@router.post("/forgot-password")
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db)
):
    """Initiate password reset flow by sending OTP to email."""
    user = await get_user_by_email(db, body.email)
    
    # We return error if user not found for better UX (as requested by user)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email address."
        )
        
    if user.account_status == AccountStatus.BLOCKED:
        raise HTTPException(status_code=403, detail="Account is suspended.")

    # Generate and store OTP (10 min expiry)
    otp = generate_otp()
    await store_otp(user.id, otp, expire_seconds=600)
    if settings.ENVIRONMENT == "development":
        logger.info(f"Generated OTP {otp} for user {user.email}")
    
    # Send email
    success = await _send_password_reset_otp_email(user.email, user.full_name, otp)
    if not success:
        logger.error(f"Failed to send password reset email to {user.email}")
    
    return {"message": "If an account exists, a reset code has been sent."}


@router.post("/verify-reset-otp")
async def verify_reset_otp(
    body: ForgotPasswordRequest, # Reuse for email
    otp: str, # This should probably be in a schema, but for brevity...
    db: AsyncSession = Depends(get_db)
):
    """Verify if the reset OTP is valid without consuming it yet."""
    user = await get_user_by_email(db, body.email)
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
    user = await get_user_by_email(db, body.email)
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
    """Return the Google OAuth authorization URL for the frontend to redirect to."""
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured.")

    uri = redirect_uri or settings.GOOGLE_REDIRECT_URI
    scope = "openid email profile"
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={settings.GOOGLE_CLIENT_ID}"
        f"&redirect_uri={uri}"
        f"&response_type=code"
        f"&scope={scope}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    if state:
        from urllib.parse import quote
        url += f"&state={quote(state)}"
    return {"url": url}


@router.post("/google/callback", response_model=TokenResponse)
async def google_callback(
    body: GoogleCallbackRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Exchange Google authorization code for tokens. Creates account if first sign-in."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured.")

    redirect_uri = body.redirect_uri or settings.GOOGLE_REDIRECT_URI

    # Step 1: Exchange auth code for Google access token
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": body.code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15.0,
        )

    if token_resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to exchange Google authorization code.")

    token_data = token_resp.json()
    google_access_token = token_data.get("access_token")

    # Step 2: Fetch Google user info
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {google_access_token}"},
            timeout=10.0,
        )

    if userinfo_resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to fetch user info from Google.")

    google_user = userinfo_resp.json()
    google_id: str = google_user.get("id")
    email: str = google_user.get("email", "")
    full_name: str = google_user.get("name", email.split("@")[0])

    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Incomplete user info from Google.")

    # Step 3: Find or create user
    user = await get_user_by_google_id(db, google_id)
    if not user:
        user = await get_user_by_email(db, email)
        if user:
            # Email matches existing account — link Google ID
            user = await link_google_to_existing(db, user, google_id)
        else:
            # Brand new Google user
            user = await create_google_user(db, full_name=full_name, email=email, google_id=google_id)

    if user.account_status == AccountStatus.BLOCKED:
        raise HTTPException(status_code=403, detail="Your account has been suspended.")

    await update_last_login(db, user)
    return _build_token_response(user, response)


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
    if old_jti and remaining > 0:
        await blacklist_token(old_jti, remaining)

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
    if body.phone_number is not None:
        current_user.phone_number = body.phone_number
    if body.avatar_url is not None:
        current_user.avatar_url = body.avatar_url

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
    )


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """
    Secure file upload to Cloudinary for any logged-in user to upload their avatar.
    """
    import cloudinary
    import cloudinary.uploader
    
    if not (settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Media storage service is not configured."
        )
        
    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/webp", "image/gif"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image files (JPG, PNG, WEBP, GIF) are allowed."
        )
        
    try:
        # Initialize Cloudinary
        cloudinary.config(
            cloud_name=settings.CLOUDINARY_CLOUD_NAME,
            api_key=settings.CLOUDINARY_API_KEY,
            api_secret=settings.CLOUDINARY_API_SECRET,
            secure=True
        )
        
        # Read the file contents
        contents = await file.read()
        
        # Upload directly to Cloudinary
        upload_result = cloudinary.uploader.upload(
            contents,
            folder="ts_tours/avatars",
            resource_type="image"
        )
        
        return {
            "url": upload_result.get("secure_url"),
            "public_id": upload_result.get("public_id"),
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload image: {str(e)}"
        )
