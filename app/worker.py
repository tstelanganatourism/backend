import asyncio
import logging
from arq import create_pool, cron
from arq.connections import RedisSettings
from app.core.config import settings
from app.services.pdf_generator import generate_package_brochure_task, process_post_booking_documents_task
from app.services.sms_service import dispatch_sms_payload
from app.workers.daily_cutoff import perform_daily_cutoff
from app.workers.missed_emails import recover_missed_emails
from app.workers.inventory_cleanup import cleanup_expired_drafts

logger = logging.getLogger(__name__)

# Parse Upstash/Redis connection string with high timeout for secure remote TLS
REDIS_SETTINGS = RedisSettings.from_dsn(settings.REDIS_URL)
REDIS_SETTINGS.conn_timeout = 15
REDIS_SETTINGS.conn_retries = 10
REDIS_SETTINGS.conn_retry_delay = 2


async def startup(ctx):
    logger.info("Worker starting up...")


async def shutdown(ctx):
    logger.info("Worker shutting down...")
    # Close the ARQ pool gracefully to prevent Redis connection leaks on restart
    global _pool
    if _pool is not None:
        try:
            await _pool.aclose()
        except Exception as e:
            logger.warning(f"Error closing ARQ pool during shutdown: {e}")
        finally:
            _pool = None
    # Dispose SQLAlchemy connection pool
    from app.db.session import engine
    await engine.dispose()
    logger.info("Database connection pool disposed gracefully.")


class WorkerSettings:
    functions = [generate_package_brochure_task, process_post_booking_documents_task, dispatch_sms_payload]
    cron_jobs = [
        # Close today's inventory slots at/after 6 AM IST & send travel day reminders.
        # Runs once on startup and every 15 minutes (instead of every minute).
        cron(perform_daily_cutoff, minute={0, 15, 30, 45}, second=15, run_at_startup=True, max_tries=1, keep_result=0),

        # Recover any emails missed while the worker was down.
        # Staggered at :05/:20/:35/:50 so it never contends with daily_cutoff.
        cron(recover_missed_emails, minute={5, 20, 35, 50}, second=0, run_at_startup=True, max_tries=1, keep_result=0),

        # Release inventory locked by expired/abandoned booking drafts every 2 minutes.
        cron(cleanup_expired_drafts, minute=set(range(0, 60, 2)), second=30, run_at_startup=False, max_tries=1, keep_result=0),
    ]
    redis_settings = REDIS_SETTINGS
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 3        # Must not exceed the DB pool ceiling
    max_tries = 3       # Retry up to 3 times if email sending fails
    job_timeout = 180   # 180 seconds max for slow external API calls
    poll_delay = 2.0    # Poll Redis every 2s instead of 0.5s (4x less idle churn)
    keep_result = 60    # Expire finished job payloads in 60s instead of 1h


_pool = None


# ARQ global pool to enqueue jobs from FastAPI
async def get_arq_pool():
    global _pool
    if _pool is None:
        _pool = await create_pool(REDIS_SETTINGS)
    return _pool
