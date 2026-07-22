from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from app.core.config import settings
from app.core.error_handlers import setup_exception_handlers
from app.core.logging import setup_logging
from loguru import logger
import sqlalchemy
import asyncio


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    pass

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    from app.db.session import engine
    await engine.dispose()
    logger.info("Database connection pool disposed gracefully.")

from app.api.v1 import auth
from app.api.v1 import public_packages, public_rooms
from app.api.v1 import promotions
from app.api.v1 import admin

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="Backend API for TS Boat Tourism Booking Platform",
    docs_url=None if settings.ENVIRONMENT == "production" else "/docs",
    redoc_url=None if settings.ENVIRONMENT == "production" else "/redoc",
    lifespan=lifespan,
)

# Initialize structured logging and error handlers
setup_logging()

if settings.SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        traces_sample_rate=1.0,
        environment=settings.ENVIRONMENT,
    )
    logger.info("Sentry initialized")

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

from app.middleware.observability import RequestIDMiddleware
app.add_middleware(RequestIDMiddleware)

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

# ─── Homepage Carousel Route ──────────────────────────────────────────────────
from app.api.v1 import carousel as carousel_module
app.include_router(
    carousel_module.router,
    prefix="/api/v1",
    tags=["Public Discovery - Carousel"],
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
from app.api.v1 import public_coupons
from app.api.v1 import public_bookings
app.include_router(
    public_coupons.router,
    prefix="/api/v1"
)
app.include_router(
    public_bookings.router,
    prefix="/api/v1/bookings"
)
from app.api.v1 import payments
app.include_router(
    payments.router,
    prefix="/api/v1/payments",
    tags=["Payments & Webhooks"]
)
from app.api.v1 import stream
app.include_router(
    stream.router,
    prefix="/api/v1"
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

from fastapi import Request, Response
from app.services.redis_client import get_redis

# --- Rate Limiting Middleware (Phase-4) --------------------------------------
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    if client_ip in ("127.0.0.1", "localhost", "::1"):
        return await call_next(request)

    path = request.url.path
    
    # Exempt routes (health, API documentation, and static assets)
    static_extensions = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf")
    if (
        path.startswith("/health")
        or path.startswith("/docs")
        or path.startswith("/openapi.json")
        or path.startswith("/static")
        or path.startswith("/_next")
        or path.endswith(static_extensions)
    ):
        return await call_next(request)

    # Determine route-based thresholds
    limit = 100        # Public browsing: 100 requests/min/IP
    category = "public"

    # OTP resend: 5-10 requests/min/IP -> set to 10
    if "/admin/resend-otp" in path or "/resend-otp" in path:
        limit = 10
        category = "otp_resend"
    
    # Admin login / OTP routes: 10-20 requests/min/IP -> set to 20
    elif (
        "/admin/login" in path
        or "/admin/verify-otp" in path
        or "/forgot-password" in path
        or "/verify-reset-otp" in path
        or "/reset-password" in path
    ):
        limit = 20
        category = "admin_login_otp"
        
    # Booking checkout: 20-30 requests/min/IP -> set to 30
    elif "/bookings" in path or "/payments" in path:
        limit = 30
        category = "checkout"

    key = f"ratelimit:{client_ip}:{category}"

    try:
        r = get_redis()
        # Redis pipeline for atomic increment and expiry
        async with r.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 60, nx=True)
            res = await pipe.execute()
            
        current = res[0]
        if current > limit:
            logger.warning(f"Rate limit exceeded for IP {client_ip} on {path} (Category: {category}, Current: {current}, Limit: {limit})")
            return Response(
                content="Too Many Requests",
                status_code=429,
            )
    except Exception as e:
        logger.error(f"Rate Limiter Redis Error: {e}")
        # Fail open if Redis is down
        pass

    return await call_next(request)
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


# reload trigger
