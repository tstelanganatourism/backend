import time
import asyncio
import httpx
from collections.abc import Awaitable, Callable
from typing import TypeVar, List
import os
from app.core.config import settings

T = TypeVar("T")

_cache: dict[str, tuple[float, object]] = {}


async def ttl_cache_get_or_set(
    key: str,
    ttl_seconds: int,
    factory: Callable[[], Awaitable[T]],
) -> T:
    now = time.monotonic()
    cached = _cache.get(key)

    if cached:
        expires_at, value = cached
        if expires_at > now:
            return value  # type: ignore[return-value]

    value = await factory()
    _cache[key] = (now + ttl_seconds, value)
    return value


def set_public_cache_headers(response, max_age: int = 60, stale_while_revalidate: int = 300) -> None:
    response.headers["Cache-Control"] = (
        f"public, max-age={max_age}, s-maxage={max_age}, "
        f"stale-while-revalidate={stale_while_revalidate}"
    )


def set_no_store_headers(response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"


def clear_cache_prefix(prefix: str) -> None:
    for key in list(_cache):
        if key.startswith(prefix):
            _cache.pop(key, None)

def trigger_frontend_revalidation(tags: List[str] = None, paths: List[str] = None) -> None:
    """
    Triggers Next.js On-Demand Revalidation via Webhook.
    """
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
            print(f"Failed to trigger revalidation: {e}")
            
    # Fire and forget
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_ping())
    except RuntimeError:
        asyncio.run(_ping())
