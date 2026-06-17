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
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
from app.core.config import settings


# ─── Singleton client ─────────────────────────────────────────────────────────

_redis_client: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    """Return the process-level Redis client, creating it on first call."""
    global _redis_client
    if _redis_client is None:
        retry_strategy = Retry(ExponentialBackoff(cap=10, base=1), 3)
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3.0,
            socket_timeout=3.0,
            max_connections=10,
            health_check_interval=10,
            retry_on_timeout=True,
            retry=retry_strategy,
            retry_on_error=[ConnectionError, TimeoutError, ConnectionResetError]
        )
    return _redis_client

_redis_client_raw: Optional[aioredis.Redis] = None

def get_redis_raw() -> aioredis.Redis:
    """Return the process-level Redis client for raw bytes (no string decoding)."""
    global _redis_client_raw
    if _redis_client_raw is None:
        retry_strategy = Retry(ExponentialBackoff(cap=10, base=1), 3)
        _redis_client_raw = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=3.0,
            socket_timeout=3.0,
            max_connections=10,
            health_check_interval=10,
            retry_on_timeout=True,
            retry=retry_strategy,
            retry_on_error=[ConnectionError, TimeoutError, ConnectionResetError]
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


# ─── SMS OTP (Phone-based, for tourist login) ────────────────────────────────
# Keys:
#   sms_otp:{phone}           — the OTP value, TTL=300s (5 min)
#   sms_otp_cooldown:{phone}  — resend cooldown lock, TTL=60s
#   sms_otp_attempts:{phone}  — attempt counter, TTL=600s (10 min window)

SMS_OTP_TTL = 300           # 5 minutes
SMS_OTP_COOLDOWN_TTL = 60   # 60 seconds between resends
SMS_OTP_MAX_ATTEMPTS = 3    # max sends per 10-min window
SMS_OTP_LOCKOUT_TTL = 600   # 10-minute lockout window


async def store_sms_otp(phone: str, otp: str) -> None:
    """Store an SMS OTP for the given phone number. TTL = 5 minutes."""
    client = get_redis()
    await client.setex(f"sms_otp:{phone}", SMS_OTP_TTL, otp)


async def verify_and_consume_sms_otp(phone: str, submitted_otp: str) -> bool:
    """
    Verify the SMS OTP for a phone number and delete it on success (single-use).
    Returns True if valid, False otherwise.
    """
    client = get_redis()
    key = f"sms_otp:{phone}"
    stored = await client.get(key)
    if stored is None or stored != submitted_otp:
        return False
    await client.delete(key)
    return True


async def check_sms_otp_rate_limit(phone: str) -> dict:
    """
    Check if a phone number is allowed to request a new OTP.
    Returns a dict with:
      allowed: bool
      cooldown_seconds: int (0 if not in cooldown)
      attempts_remaining: int
      locked: bool
    """
    client = get_redis()
    cooldown_key = f"sms_otp_cooldown:{phone}"
    attempts_key = f"sms_otp_attempts:{phone}"

    attempts_str = await client.get(attempts_key)
    attempts = int(attempts_str) if attempts_str else 0

    if attempts >= SMS_OTP_MAX_ATTEMPTS:
        ttl = await client.ttl(attempts_key)
        return {"allowed": False, "cooldown_seconds": max(0, ttl), "attempts_remaining": 0, "locked": True}

    cooldown_ttl = await client.ttl(cooldown_key)
    in_cooldown = cooldown_ttl > 0

    if in_cooldown:
        return {"allowed": False, "cooldown_seconds": cooldown_ttl, "attempts_remaining": SMS_OTP_MAX_ATTEMPTS - attempts, "locked": False}

    return {"allowed": True, "cooldown_seconds": 0, "attempts_remaining": SMS_OTP_MAX_ATTEMPTS - attempts, "locked": False}


async def record_sms_otp_send(phone: str) -> None:
    """
    Record that an OTP was sent to this phone.
    Increments the attempt counter and sets the 60s cooldown.
    """
    client = get_redis()
    attempts_key = f"sms_otp_attempts:{phone}"
    cooldown_key = f"sms_otp_cooldown:{phone}"

    # Increment attempt counter; set TTL only on first increment
    current = await client.incr(attempts_key)
    if current == 1:
        await client.expire(attempts_key, SMS_OTP_LOCKOUT_TTL)

    # Set cooldown lock
    await client.setex(cooldown_key, SMS_OTP_COOLDOWN_TTL, "1")
