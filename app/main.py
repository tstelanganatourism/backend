from contextlib import asynccontextmanager
import os
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from app.core.config import settings
from app.core.error_handlers import setup_exception_handlers
from app.core.logging import setup_logging
from loguru import logger
import sqlalchemy
import asyncio


def _get_rss_mb() -> float | None:
    """Return resident/peak RSS in MB on POSIX hosts without external psutil dependency."""
    try:
        import resource
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux returns KB, macOS returns bytes
        if sys.platform == "darwin":
            return round(maxrss / (1024 * 1024), 1)
        return round(maxrss / 1024, 1)
    except Exception:
        return None


async def _run_embedded_worker_forever(arq_worker) -> None:
    """Run embedded ARQ worker inside FastAPI lifespan with resilient reconnect backoff."""
    while True:
        try:
            await arq_worker.main()
            return
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning(f"Embedded ARQ worker loop error ({err}); restarting in 5s...")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    # Warmup database connection pool & Redis to eliminate cold-start latencies
    try:
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await session.execute(sqlalchemy.text("SELECT 1"))
        logger.info("Database pool warmed up successfully.")
    except Exception as e:
        logger.warning(f"Database warmup warning: {e}")

    try:
        from app.services.redis_client import get_redis_raw
        r = get_redis_raw()
        await asyncio.wait_for(r.ping(), timeout=1.0)
        logger.info("Redis connection warmed up successfully.")
    except Exception as e:
        logger.warning(f"Redis warmup warning: {e}")

    # Warmup memory cache for packages and rooms to deliver < 1ms TTFB
    try:
        from app.services.cache_warmer import warmup_public_cache
        asyncio.create_task(warmup_public_cache())
    except Exception as e:
        logger.warning(f"Failed to schedule cache warmup: {e}")

    # Single embedded ARQ worker handles all background queues (emails, SMS, brochures)
    # AND all scheduled crons (draft cleanup, daily cutoff, missed email recovery),
    # eliminating duplicate Python processes and duplicate periodic asyncio loops.
    arq_worker = None
    arq_task = None
    if settings.ENABLE_EMBEDDED_WORKER:
        try:
            from arq.worker import create_worker
            from app.worker import WorkerSettings
            arq_worker = create_worker(WorkerSettings)
            arq_task = asyncio.create_task(_run_embedded_worker_forever(arq_worker))
            logger.info("Embedded ARQ background worker started inside FastAPI.")
        except Exception as e:
            logger.warning(f"Failed to start embedded ARQ worker: {e}")

    logger.info(f"Startup complete (peak RSS: {_get_rss_mb()} MB).")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    if arq_worker:
        try:
            await arq_worker.close()
        except Exception:
            pass
    if arq_task:
        arq_task.cancel()
        try:
            await arq_task
        except asyncio.CancelledError:
            pass

    try:
        from app.services.r2_storage import r2_service
        await r2_service.close()
    except Exception:
        pass

    from app.db.session import engine
    await engine.dispose()
    logger.info("Database connection pool disposed gracefully.")

from app.api.v1 import auth
from app.api.v1 import public_packages, public_rooms
from app.api.v1 import promotions
from app.api.v1 import admin

import os
from fastapi.staticfiles import StaticFiles

# Create local static uploads directory
static_dir = os.path.join(os.path.dirname(__file__), "static")
uploads_dir = os.path.join(static_dir, "uploads")
os.makedirs(uploads_dir, exist_ok=True)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="Backend API for TS Boat Tourism Booking Platform",
    docs_url=None if settings.ENVIRONMENT == "production" else "/docs",
    redoc_url=None if settings.ENVIRONMENT == "production" else "/redoc",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Initialize structured logging and error handlers
setup_logging()

if settings.SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        environment=settings.ENVIRONMENT,
    )
    logger.info("Sentry initialized")

setup_exception_handlers(app)

from fastapi import Response
from starlette.types import ASGIApp, Receive, Scope, Send
from app.services.redis_client import get_redis


class RateLimitMiddleware:
    """
    Pure ASGI Redis rate limiting middleware.
    Avoids Starlette BaseHTTPMiddleware request/response body buffering on file uploads and SSE streams.
    """

    STATIC_EXTENSIONS = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf")

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if (
            path.startswith("/health")
            or path.startswith("/ping")
            or path.startswith("/docs")
            or path.startswith("/openapi.json")
            or path.startswith("/static")
            or path.startswith("/_next")
            or path.endswith(self.STATIC_EXTENSIONS)
        ):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        forwarded = headers.get(b"x-forwarded-for")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client = scope.get("client")
            client_ip = headers.get(b"cf-connecting-ip") or (client[0] if client else "unknown")

        if client_ip in ("127.0.0.1", "localhost", "::1"):
            await self.app(scope, receive, send)
            return

        limit = 200
        category = "public"

        if path.startswith("/api/v1/admin"):
            if "/admin/resend-otp" in path:
                limit = 10
                category = "otp_resend"
            elif "/admin/login" in path or "/admin/verify-otp" in path:
                limit = 20
                category = "admin_login_otp"
            else:
                limit = 600
                category = "admin_portal"
        elif "/admin/resend-otp" in path or "/resend-otp" in path:
            limit = 10
            category = "otp_resend"
        elif (
            "/admin/login" in path
            or "/admin/verify-otp" in path
            or "/forgot-password" in path
            or "/verify-reset-otp" in path
            or "/reset-password" in path
        ):
            limit = 20
            category = "admin_login_otp"
        elif method in ("POST", "PUT") and ("/bookings" in path or "/payments" in path or "/checkout" in path):
            limit = 60
            category = "checkout"

        auth_header = headers.get(b"authorization")
        if auth_header and auth_header.startswith("Bearer "):
            try:
                from app.core.security import decode_token
                token_str = auth_header.split(" ", 1)[1]
                token_payload = decode_token(token_str, expected_type="access")
                if token_payload.get("role") == "ADMIN":
                    limit = max(limit, 600)
                    category = "admin_authenticated"
            except Exception:
                pass

        key = f"ratelimit:{client_ip}:{category}"
        try:
            r = get_redis()
            async with r.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, 60, nx=True)
                res = await pipe.execute()
            current = res[0]
            if current > limit:
                logger.warning(f"Rate limit exceeded for IP {client_ip} on {path} (Category: {category}, Current: {current}, Limit: {limit})")
                resp = Response(
                    content='{"detail": "Too many requests. Please slow down and try again shortly."}',
                    status_code=429,
                    media_type="application/json",
                )
                await resp(scope, receive, send)
                return
        except Exception as e:
            logger.error(f"Rate Limiter Redis Error: {e}")

        await self.app(scope, receive, send)


from fastapi.middleware.gzip import GZipMiddleware

app.add_middleware(
    GZipMiddleware,
    minimum_size=500,
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.ALLOWED_HOSTS,
)

app.add_middleware(RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_origin_regex=r"https://.*\.vercel\.app",
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
from app.api.v1 import pre_bookings as public_pre_bookings
app.include_router(
    public_coupons.router,
    prefix="/api/v1"
)
app.include_router(
    public_bookings.router,
    prefix="/api/v1/bookings"
)
app.include_router(
    public_pre_bookings.router,
    prefix="/api/v1/pre-bookings",
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
from app.api.v1 import activity

# ─── Documents Routes (Phase-3) ───────────────────────────────────────────────────
app.include_router(
    documents.router,
    prefix="/api/v1",
)

# ─── Activity Funnel Routes ───────────────────────────────────────────────────
app.include_router(
    activity.router,
    prefix="/api/v1/activity",
    tags=["User Activity & Funnel Tracking"],
)

# ─── Admin Routes (Phase-3) ───────────────────────────────────────────────────
app.include_router(
    admin.router,
    prefix="/api/v1/admin",
)

@app.get("/ping")
async def ping():
    """Zero-overhead liveness ping for Render keep-alive (0 DB, 0 Redis, < 0.1ms)."""
    return {"status": "ok"}

@app.get("/health")
async def health_check():
    """Robust health check verifying external dependencies."""
    health_status = {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "db": "unknown",
        "redis": "unknown",
        "peak_rss_mb": _get_rss_mb(),
    }
    
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
