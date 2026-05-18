from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from app.core.config import settings
from app.core.error_handlers import setup_exception_handlers
from app.core.logging import setup_logging
from loguru import logger
import sqlalchemy

from app.api.v1 import auth
from app.api.v1 import public_packages, public_rooms
from app.api.v1 import promotions
from app.api.v1 import admin

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="Backend API for Papikondalu Tourism Booking Platform",
    docs_url=None if settings.ENVIRONMENT == "production" else "/docs",
    redoc_url=None if settings.ENVIRONMENT == "production" else "/redoc",
)

# Initialize structured logging and error handlers
setup_logging()
setup_exception_handlers(app)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.ALLOWED_HOSTS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Public Discovery Routes ──────────────────────────────────────────────────
app.include_router(
    public_packages.router,
    prefix="/api/v1/packages",
    tags=["Public Discovery - Packages"],
)
app.include_router(
    public_rooms.router,
    prefix="/api/v1/rooms",
    tags=["Public Discovery - Rooms"],
)

# ─── Auth Routes (Phase-3) ────────────────────────────────────────────────────
app.include_router(
    auth.router,
    prefix="/api/v1/auth",
    tags=["Authentication"],
)

# ─── Promotions Routes (Phase-3) ──────────────────────────────────────────────
app.include_router(
    promotions.router,
    prefix="/api/v1/promotions",
    tags=["Promotions"],
)
from app.api.v1 import documents

# ─── Documents Routes (Phase-3) ───────────────────────────────────────────────────
app.include_router(
    documents.router,
    prefix="/api/v1",
)

# ─── Admin Routes (Phase-3) ───────────────────────────────────────────────────
app.include_router(
    admin.router,
    prefix="/api/v1/admin",
)

# --- Rate Limiting Scaffold (Phase-4) ----------------------------------------
# @app.middleware("http")
# async def rate_limit_middleware(request, call_next):
#     response = await call_next(request)
#     return response
# -----------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    """Robust health check verifying external dependencies."""
    health_status = {"status": "ok", "service": settings.PROJECT_NAME, "db": "unknown", "redis": "unknown"}
    
    # 1. Check DB
    try:
        from app.db.session import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        health_status["db"] = "ok"
    except Exception as e:
        logger.error(f"DB Health Check Failed: {e}")
        health_status["db"] = "error"
        health_status["status"] = "degraded"
        
    # 2. Check Redis
    try:
        from app.services.redis_client import get_redis
        r = get_redis()
        await r.ping()
        health_status["redis"] = "ok"
    except Exception as e:
        logger.error(f"Redis Health Check Failed: {e}")
        health_status["redis"] = "error"
        health_status["status"] = "degraded"
        
    if health_status["status"] == "degraded":
        from fastapi import Response
        import json
        return Response(content=json.dumps(health_status), status_code=503, media_type="application/json")
        
    return health_status




