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
            socket_connect_timeout=3.0,
            socket_timeout=3.0,
        )
    return _redis_client

_redis_client_raw: Optional[aioredis.Redis] = None

def get_redis_raw() -> aioredis.Redis:
    """Return the process-level Redis client for raw bytes (no string decoding)."""
    global _redis_client_raw
    if _redis_client_raw is None:
        _redis_client_raw = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=3.0,
            socket_timeout=3.0,
        )
    return _redis_client_raw


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


import time

async def blacklist_token(jti: str, expire_seconds: int, is_logout: bool = False) -> None:
    """Blacklist a JWT by its JTI. TTL matches original token expiry."""
    client = get_redis()
    key = f"blacklist:{jti}"
    value = "logout" if is_logout else str(time.time())
    await client.setex(key, expire_seconds, value)


async def is_token_blacklisted(jti: str) -> bool:
    """Return True if the given JTI has been blacklisted (i.e., token revoked)."""
    try:
        client = get_redis()
        key = f"blacklist:{jti}"
        value = await client.get(key)
        if not value:
            return False
        if value == "logout":
            return True
        
        # Grace period logic: bypass blacklist for 30s during rotation race conditions
        try:
            blacklist_time = float(value)
            if time.time() - blacklist_time < 30.0:
                return False
        except ValueError:
            pass
            
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Redis error checking token blacklist (fail-open): {e}")
        return False


# ─── Package Availability Caching ────────────────────────────────────────────

import json

async def get_cached_availability(slug: str, month: str) -> Optional[dict]:
    """Retrieve cached package availability JSON from Redis."""
    try:
        client = get_redis()
        key = f"pkg_availability:{slug}:{month}"
        data = await client.get(key)
        if data:
            return json.loads(data)
    except Exception:
        pass
    return None

async def set_cached_availability(slug: str, month: str, data: dict, ttl_seconds: int = 60) -> None:
    """Cache package availability JSON in Redis with a TTL."""
    try:
        client = get_redis()
        key = f"pkg_availability:{slug}:{month}"
        await client.setex(key, ttl_seconds, json.dumps(data))
    except Exception:
        pass

async def invalidate_cached_availability(slug: str) -> None:
    """Invalidate all cached availability keys for a package slug."""
    try:
        client = get_redis()
        cursor = 0
        match = f"pkg_availability:{slug}:*"
        while True:
            cursor, keys = await client.scan(cursor, match=match)
            if keys:
                await client.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass

