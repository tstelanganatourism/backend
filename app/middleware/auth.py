"""
Auth middleware — FastAPI dependency-injection guards.

Usage in route handlers:
    current_user = Depends(get_current_user)        # any authenticated user
    _            = Depends(require_tourist)          # role=USER only
    _            = Depends(require_agent_or_admin)   # role=AGENT or ADMIN
    _            = Depends(require_admin)            # role=ADMIN only
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import User
from app.models.enums import UserRole, AccountStatus
from app.repositories.user_repository import get_user_by_id
from app.services.redis_client import is_token_blacklisted

from typing import Optional

# FastAPI bearer scheme — auto-pulls from Authorization: Bearer <token>
bearer_scheme = HTTPBearer(auto_error=True)
bearer_scheme_optional = HTTPBearer(auto_error=False)


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme_optional),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """
    Optionally fetch the current authenticated user if the Bearer token is provided.
    If a token is provided but is invalid/expired, it raises a 401 so the frontend
    can trigger a token refresh, rather than silently failing to anonymous.
    """
    if not credentials:
        return None
        
    # If a token is provided, it MUST be valid, otherwise we want the 401 interceptor to catch it.
    token = credentials.credentials
    try:
        payload = decode_token(token, expected_type="access")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid or expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    jti: str = payload.get("jti", "")
    if jti and await is_token_blacklisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    user_id: int = int(payload["sub"])
    user = await get_user_by_id(db, user_id)
    
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found.",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Validate the access token and return the authenticated User.
    Raises HTTP 401 on invalid/expired token or blacklisted JTI.
    Raises HTTP 403 if the account is blocked/disabled.
    """
    token = credentials.credentials
    payload = decode_token(token, expected_type="access")

    jti: str = payload.get("jti", "")
    if jti and await is_token_blacklisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: int = int(payload["sub"])
    user = await get_user_by_id(db, user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def require_role(*roles: UserRole):
    """
    Factory that produces a dependency requiring the current user to have
    one of the specified roles. Use as:
        Depends(require_role(UserRole.ADMIN))
    """
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role(s): {[r.value for r in roles]}",
            )
        return current_user
    return role_checker


# ─── Pre-built dependency shortcuts ──────────────────────────────────────────

require_tourist = require_role(UserRole.USER)
require_agent = require_role(UserRole.AGENT)
require_agent_or_admin = require_role(UserRole.AGENT, UserRole.ADMIN)
require_admin = require_role(UserRole.ADMIN)
