from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
import sqlalchemy
from app.core.config import settings

import sys

import os
# ─── Detect environment ───────────────────────────────────────────────────────
# True production runs on Render or has ENVIRONMENT='production'
is_production = settings.ENVIRONMENT == "production" or os.getenv("RENDER") is not None
is_local_dev = not is_production

# Determine if we are running as a background worker (arq command)
is_worker = any("arq" in arg or "worker" in arg for arg in sys.argv)

# ─── Connection pool configuration ───────────────────────────────────────────
# Total Postgres ceiling on Aiven free tier is 20 connections max.
# We keep pool sizes lean, timeouts tight (5s), and statement/idle timeouts low
# so connections are released instantly and never cause queue blockades.
if is_local_dev:
    pool_kwargs = {
        "pool_size": 3,
        "max_overflow": 2,
        "pool_timeout": 5,
        "pool_recycle": 180,
        "pool_pre_ping": True,
    }
elif is_worker:
    # ── Production ARQ worker ─────────────────────────────────────────────────
    pool_kwargs = {
        "pool_size": 2,
        "max_overflow": 1,
        "pool_timeout": 5,
        "pool_recycle": 180,
        "pool_pre_ping": True,
    }
else:
    # 🌍 Production web server ───────────────────────────────────────────────
    pool_kwargs = {
        "pool_size": 5,
        "max_overflow": 3,
        "pool_timeout": 5,
        "pool_recycle": 180,
        "pool_pre_ping": True,
    }

# Create async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    connect_args={
        "prepared_statement_cache_size": 0,
        "server_settings": {
            "jit": "off",
            "idle_in_transaction_session_timeout": "5000",
            "statement_timeout": "15000"
        }
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
