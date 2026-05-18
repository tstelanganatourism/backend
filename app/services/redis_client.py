"""
Async Redis client singleton for the application.

Responsibilities:
  - OTP storage and verification for admin login
  - Access/Refresh token blacklisting for logout
  - Health-check connectivity (reused in main.py)
"""
from datetime import timedelta
from typing import Optional

import redis.asyncio as aioredis
from app.core.config import settings


# ─── Singleton client ─────────────────────────────────────────────────────────

_redis_client: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    """Return the process-level Redis client, creating it on first call."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


# ─── OTP Operations ──────────────────────────────────────────────────────────

async def store_otp(user_id: int, otp: str, expire_seconds: Optional[int] = None) -> None:
    """Store an OTP in Redis with the configured TTL."""
    client = get_redis()
    key = f"otp:{user_id}"
    ttl = expire_seconds if expire_seconds is not None else settings.ADMIN_OTP_EXPIRE_SECONDS
    await client.setex(key, ttl, otp)


async def verify_and_consume_otp(user_id: int, submitted_otp: str) -> bool:
    """
    Verify an OTP and delete it on success (single-use).
    Returns True if valid, False otherwise.
    """
    client = get_redis()
    key = f"otp:{user_id}"
    stored_otp = await client.get(key)
    if stored_otp is None:
        return False
    if stored_otp != submitted_otp:
        return False
    # Consumed — delete immediately so it cannot be reused
    await client.delete(key)
    return True


async def verify_otp_only(user_id: int, submitted_otp: str) -> bool:
    """Verify an OTP without deleting it (for intermediate validation)."""
    client = get_redis()
    key = f"otp:{user_id}"
    stored_otp = await client.get(key)
    return bool(stored_otp and stored_otp == submitted_otp)


# ─── Token Blacklist Operations ───────────────────────────────────────────────

async def blacklist_token(jti: str, expire_seconds: int) -> None:
    """Blacklist a JWT by its JTI. TTL matches original token expiry."""
    client = get_redis()
    key = f"blacklist:{jti}"
    await client.setex(key, expire_seconds, "1")


async def is_token_blacklisted(jti: str) -> bool:
    """Return True if the given JTI has been blacklisted (i.e., token revoked)."""
    client = get_redis()
    key = f"blacklist:{jti}"
    result = await client.exists(key)
    return bool(result)
