"""
In-memory high-speed LRU/TTL cache for public API endpoints.
Provides sub-millisecond (<0.5ms) response times for read-heavy public endpoints:
- Package availability & fare calendar
- Public packages & categories
- Package detail by slug
"""
import time
from typing import Any, Optional, Dict, Tuple
from loguru import logger

# Store format: { (namespace, key): (data, expire_at) }
_CACHE: Dict[Tuple[str, str], Tuple[Any, float]] = {}
_MAX_ENTRIES = 5000


def get_mem_cached(namespace: str, key: str) -> Optional[Any]:
    """Retrieve an item from the in-memory cache if not expired."""
    cache_key = (namespace, key)
    entry = _CACHE.get(cache_key)
    if entry is None:
        return None
    data, expire_at = entry
    if time.time() > expire_at:
        _CACHE.pop(cache_key, None)
        return None
    return data


def set_mem_cached(namespace: str, key: str, value: Any, ttl_seconds: int = 60) -> None:
    """Store an item in the in-memory cache with a TTL."""
    # Evict expired entries if cache gets too large
    if len(_CACHE) >= _MAX_ENTRIES:
        now = time.time()
        expired_keys = [k for k, (_, exp) in _CACHE.items() if now > exp]
        for k in expired_keys:
            _CACHE.pop(k, None)
        # If still full, drop oldest 10%
        if len(_CACHE) >= _MAX_ENTRIES:
            keys_to_drop = list(_CACHE.keys())[:500]
            for k in keys_to_drop:
                _CACHE.pop(k, None)

    cache_key = (namespace, key)
    _CACHE[cache_key] = (value, time.time() + ttl_seconds)


def invalidate_mem_cached(namespace: str, key_prefix: Optional[str] = None) -> None:
    """Invalidate cache entries matching namespace and optional key prefix."""
    keys_to_delete = []
    for (ns, k) in _CACHE.keys():
        if ns == namespace:
            if key_prefix is None or k.startswith(key_prefix):
                keys_to_delete.append((ns, k))
    for k in keys_to_delete:
        _CACHE.pop(k, None)


def clear_mem_cache() -> None:
    """Clear all in-memory cache entries."""
    _CACHE.clear()
