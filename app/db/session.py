from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings

from sqlalchemy.pool import NullPool

import sys

# Determine if we are running as a background worker (arq command)
is_worker = any("arq" in arg or "worker" in arg for arg in sys.argv)

if is_worker:
    pool_size = 5
    max_overflow = 5
else:
    pool_size = 20
    max_overflow = 20

# Create async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    pool_size=pool_size,
    max_overflow=max_overflow,
    pool_timeout=30,
    pool_recycle=900,
    pool_pre_ping=True,
    connect_args={
        "prepared_statement_cache_size": 0,
        "server_settings": {"jit": "off"}
    }
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
# Force uvicorn reload comment for clearing in-memory cache
