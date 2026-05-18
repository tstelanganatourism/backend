"""
User repository — async database operations for the users table.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.enums import UserRole, AccountStatus
from app.core.security import get_password_hash
from app.core.timezone import get_ist_now


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    """Fetch a user by email address. Returns None if not found."""
    result = await db.execute(
        select(User).where(User.email == email, User.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: int) -> Optional[User]:
    """Fetch a user by primary key. Returns None if not found."""
    result = await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def get_user_by_google_id(db: AsyncSession, google_id: str) -> Optional[User]:
    """Fetch a user linked to a Google account. Returns None if not found."""
    result = await db.execute(
        select(User).where(User.google_id == google_id, User.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def create_tourist_user(
    db: AsyncSession,
    *,
    full_name: str,
    email: str,
    password: str,
    phone_number: Optional[str] = None,
) -> User:
    """Create a new tourist account. Hashes password before storage."""
    user = User(
        full_name=full_name,
        email=email,
        password_hash=get_password_hash(password),
        phone_number=phone_number,
        role=UserRole.USER,
        account_status=AccountStatus.ACTIVE,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def create_google_user(
    db: AsyncSession,
    *,
    full_name: str,
    email: str,
    google_id: str,
) -> User:
    """Create a tourist account from Google OAuth (no password)."""
    user = User(
        full_name=full_name,
        email=email,
        google_id=google_id,
        password_hash=None,
        role=UserRole.USER,
        account_status=AccountStatus.ACTIVE,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def link_google_to_existing(
    db: AsyncSession,
    user: User,
    google_id: str,
) -> User:
    """Link a Google ID to an existing account (first-time Google sign-in for email match)."""
    user.google_id = google_id
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def update_last_login(db: AsyncSession, user: User) -> None:
    """Update the last_login timestamp for a user (fire-and-forget pattern)."""
    user.last_login = get_ist_now()
    db.add(user)
    await db.commit()


async def update_user_password(db: AsyncSession, user: User, new_password: str) -> None:
    """Hash and update the user's password."""
    user.password_hash = get_password_hash(new_password)
    db.add(user)
    await db.commit()
    await db.refresh(user)
