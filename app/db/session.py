from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
import sqlalchemy
from app.core.config import settings

import sys

# ─── Detect environment ───────────────────────────────────────────────────────
# If FRONTEND_URL points to localhost, we are running locally for development.
# This lets us apply a minimal connection pool so local dev never eats into
# the production database's connection ceiling (Aiven free tier: ~15 max).
is_local_dev = "localhost" in (settings.FRONTEND_URL or "")

# Determine if we are running as a background worker (arq command)
is_worker = any("arq" in arg or "worker" in arg for arg in sys.argv)

# ─── Connection pool configuration ───────────────────────────────────────────
#
# Aiven Free Tier: ~15 total connections, last few reserved for SUPERUSER.
#
# LOCAL DEV  → bare minimum (1 each) so local dev never hogs prod slots.
#              Total local: 2 connections max.
#
# PRODUCTION → generous pool for real traffic:
#   Web server : pool_size=5, max_overflow=3  →  8 max connections
#   ARQ worker : pool_size=2, max_overflow=1  →  3 max connections
#   Total prod : 11 connections max  (4 left as superuser headroom)
#
if is_local_dev:
    # NullPool allows UNLIMITED concurrent connections during spikes.
    # We MUST use a small QueuePool to enforce a hard ceiling on connections.
    # Reduced to 3 max — local dev and production share the same Aiven DB instance
    # (15 connections total). Prod web=10 + worker=3 = 13 used, leaving 2 for dev.
    pool_kwargs = {
        "pool_size": 2,
        "max_overflow": 1,
        "pool_timeout": 30,
        "pool_recycle": 600,
        "pool_pre_ping": True,
    }
elif is_worker:
    # ── Production ARQ worker ─────────────────────────────────────────────────
    pool_kwargs = {
        "pool_size": 2,
        "max_overflow": 1,
        "pool_timeout": 30,
        "pool_recycle": 600,
        "pool_pre_ping": True,
    }
else:
    # 🌍 Production web server ───────────────────────────────────────────────
    # Safely allocate connections for production (pool_size=6, max_overflow=4 = 10 total)
    # This leaves 3 connections for the ARQ worker and 2 for superusers (15 total limit)
    pool_kwargs = {
        "pool_size": 6,
        "max_overflow": 4,
        "pool_timeout": 30,
        "pool_recycle": 600,
        "pool_pre_ping": True,
    }

# Create async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    connect_args={
        "prepared_statement_cache_size": 0,
        "server_settings": {"jit": "off"}
    },
    **pool_kwargs
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

from app.db.base import Base

# Dependency to get DB session
async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
