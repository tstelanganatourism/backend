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
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, UploadFile, File
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
        },
    }


# ─── Send OTP email via Resend ────────────────────────────────────────────────

async def _send_password_reset_otp_email(email: str, full_name: str, otp: str):
    """Send a password reset OTP email via Resend."""
    if not settings.RESEND_API_KEY:
        return

    payload = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [email],
        "subject": "Password Reset Code - TS Tourism",
        "html": f"""
        <div style="font-family: sans-serif; max-width: 500px; margin: 0 auto; border: 1px solid #eee; padding: 40px; border-radius: 16px;">
          <h2 style="color: #0f3d56; margin-top: 0;">Password Reset</h2>
          <p style="color: #555;">Hello {full_name},</p>
          <p style="color: #555;">You requested a password reset. Use the following code to set a new password:</p>
          <div style="background: #fdf2f2; border-radius: 12px; padding: 24px; text-align: center; margin: 24px 0;">
            <span style="font-size: 36px; font-weight: 900; letter-spacing: 12px; color: #dc2626;">{otp}</span>
          </div>
          <p style="color: #888; font-size: 13px;">This code expires in 10 minutes. If you did not request this, please secure your account.</p>
          <hr style="border: 0; border-top: 1px solid #eee; margin: 24px 0;" />
          <p style="color: #bbb; font-size: 11px;">TS Tourism &bull; Account Security</p>
        </div>
        """,
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.error(f"Resend API Error (Password Reset): {resp.status_code} - {resp.text}")
                return False
            logger.info(f"Password reset email sent to {email} successfully.")
            return True
    except Exception as e:
        logger.error(f"Failed to send password reset email: {str(e)}")
        return False


async def _send_admin_otp_email(email: str, full_name: str, otp: str):
    """Send an OTP email via Resend."""
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

    if not settings.RESEND_API_KEY:
        logger.warning("No RESEND_API_KEY found, skipping actual email send.")
        return

    payload = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [email],
        "subject": "Verification Code - TS Tourism Admin",
        "html": f"""
        <div style="font-family: sans-serif; max-width: 500px; margin: 0 auto; border: 1px solid #eee; padding: 40px; border-radius: 16px;">
          <h2 style="color: #0f3d56; margin-top: 0;">Admin Verification</h2>
          <p style="color: #555;">Hello {full_name},</p>
          <p style="color: #555;">Your one-time security code to access the TS Tourism Admin Portal is:</p>
          <div style="background: #f0f9ff; border-radius: 12px; padding: 24px; text-align: center; margin: 24px 0;">
            <span style="font-size: 36px; font-weight: 900; letter-spacing: 12px; color: #0f3d56;">{otp}</span>
          </div>
          <p style="color: #888; font-size: 13px;">This code expires in 5 minutes. If you did not request this, please ignore this email.</p>
          <hr style="border: 0; border-top: 1px solid #eee; margin: 24px 0;" />
          <p style="color: #bbb; font-size: 11px;">TS Tourism Services &bull; Secure Admin Portal</p>
        </div>
        """,
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.error(f"Resend API Error: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send OTP email: {str(e)}")


@router.post("/admin/resend-otp")
async def admin_resend_otp(
    body: AdminLoginRequest, # Reuse email/password for security context
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
    await _send_admin_otp_email(user.email, user.full_name, otp)
    
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
    await _send_admin_otp_email(user.email, user.full_name, otp)

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
                await blacklist_token(jti, remaining)
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
