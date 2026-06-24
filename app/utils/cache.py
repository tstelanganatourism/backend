import time
import asyncio
import httpx
import pickle
from collections.abc import Awaitable, Callable
from typing import TypeVar, List, Optional
import os
from app.core.config import settings

T = TypeVar("T")

# ─── L1: In-Process Memory Cache ─────────────────────────────────────────────
# Avoids Redis entirely for hot, recently-served pages.
# Each entry is (value, expires_at). Max 500 entries (LRU-style via insertion order).

_mem_cache: dict[str, tuple] = {}  # key -> (value, expires_at)
_mem_cache_max = 500

def _mem_get(key: str) -> Optional[object]:
    entry = _mem_cache.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if time.monotonic() >= expires_at:
        _mem_cache.pop(key, None)
        return None
    return value

def _mem_set(key: str, value: object, ttl_seconds: int) -> None:
    if len(_mem_cache) >= _mem_cache_max:
        # Evict oldest 50 entries
        for old_key in list(_mem_cache.keys())[:50]:
            _mem_cache.pop(old_key, None)
    _mem_cache[key] = (value, time.monotonic() + ttl_seconds)

def _mem_delete_prefix(prefix: str) -> None:
    keys_to_delete = [k for k in _mem_cache if k.startswith(prefix)]
    for k in keys_to_delete:
        _mem_cache.pop(k, None)


# ─── Main Cache Utility ───────────────────────────────────────────────────────

async def ttl_cache_get_or_set(
    key: str,
    ttl_seconds: int,
    factory: Callable[[], Awaitable[T]],
) -> T:
    # 1. L1 fast path: in-process memory cache (no network, < 1ms)
    cached = _mem_get(key)
    if cached is not None:
        return cached

    # 2. L2: Try Redis with short timeout
    redis_available = False
    try:
        from app.services.redis_client import get_redis_raw
        redis = get_redis_raw()
        cached_data = await asyncio.wait_for(redis.get(key), timeout=0.8)
        if cached_data:
            value = pickle.loads(cached_data)
            _mem_set(key, value, ttl_seconds)
            return value
        redis_available = True
    except Exception:
        redis_available = False

    # 3. Cache miss: Execute factory (DB query)
    value = await factory()

    # 4. Populate L1 cache immediately
    _mem_set(key, value, ttl_seconds)

    # 5. Populate L2 Redis cache in background (non-blocking)
    if redis_available:
        async def _save_to_redis():
            try:
                from app.services.redis_client import get_redis_raw
                r = get_redis_raw()
                await asyncio.wait_for(
                    r.setex(key, ttl_seconds, pickle.dumps(value)),
                    timeout=1.0
                )
            except Exception:
                pass
        asyncio.create_task(_save_to_redis())

    return value


def invalidate_mem_cache_prefix(prefix: str) -> None:
    """Invalidate all in-process memory cache entries matching prefix."""
    _mem_delete_prefix(prefix)


def set_public_cache_headers(response, max_age: int = 60, stale_while_revalidate: int = 300) -> None:
    response.headers["Cache-Control"] = (
        f"public, max-age={max_age}, s-maxage={max_age}, "
        f"stale-while-revalidate={stale_while_revalidate}"
    )

def set_no_store_headers(response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"

def clear_cache_prefix(prefix: str) -> None:
    """
    Clears Redis keys matching prefix AND the in-process memory cache.
    Fires Redis clear as a background task to prevent blocking.
    """
    # Always clear memory cache immediately
    _mem_delete_prefix(prefix)

    async def _clear():
        try:
            from app.services.redis_client import get_redis_raw
            redis = get_redis_raw()
            keys = await asyncio.wait_for(redis.keys(f"{prefix}*"), timeout=1.0)
            if keys:
                await asyncio.wait_for(redis.delete(*keys), timeout=1.0)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to clear cache prefix {prefix}: {e}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_clear())
    except RuntimeError:
        asyncio.run(_clear())

async def clear_cache_prefix_async(prefix: str) -> None:
    """
    Clears Redis keys matching prefix AND the in-process memory cache.
    Awaits the Redis delete operation to prevent race conditions.
    """
    _mem_delete_prefix(prefix)
    try:
        from app.services.redis_client import get_redis_raw
        redis = get_redis_raw()
        keys = await asyncio.wait_for(redis.keys(f"{prefix}*"), timeout=1.0)
        if keys:
            await asyncio.wait_for(redis.delete(*keys), timeout=1.0)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to clear cache prefix {prefix} async: {e}")

def trigger_frontend_revalidation(tags: List[str] = None, paths: List[str] = None) -> None:
    """
    Triggers Next.js On-Demand Revalidation via Webhook.
    Bypassed in development mode to avoid Next.js page compilation lags.
    """
    if os.getenv("ENVIRONMENT", "development") == "development":
        return

    frontend_url = settings.FRONTEND_URL
    url = f"{frontend_url}/api/revalidate"
    payload = {
        "secret": os.getenv("REVALIDATE_SECRET", "ts-tourism-revalidate-2024"),
        "tags": tags or [],
        "paths": paths or []
    }
    
    async def _ping():
        try:
            async with httpx.AsyncClient() as client:
                await client.post(url, json=payload, timeout=5.0)
        except Exception as e:
            pass
            
    # Fire and forget
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_ping())
    except RuntimeError:
        asyncio.run(_ping())
