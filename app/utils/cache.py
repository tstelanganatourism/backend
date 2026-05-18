import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

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
