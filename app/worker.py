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
        # Close today's inventory slots at 6 AM IST (runs every minute, idempotent via Redis key)
        cron(perform_daily_cutoff, second=0, run_at_startup=True),

        # Recover any emails missed while the worker was down.
        # Runs immediately on startup and then every 15 minutes.
        cron(recover_missed_emails, minute={0, 15, 30, 45}, run_at_startup=True),

        # Release inventory locked by expired/abandoned booking drafts.
        # Runs every 5 minutes. Prevents seats being stuck as "reserved" forever.
        cron(cleanup_expired_drafts, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, run_at_startup=False),
    ]
    redis_settings = REDIS_SETTINGS
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 3   # Must not exceed the ARQ worker's DB pool ceiling (pool_size=2, max_overflow=1)
    max_tries = 3  # Retry up to 3 times if email sending fails
    job_timeout = 300   # 300 seconds to handle slow Brevo, DB, or Playwright PDF generation


_pool = None


# ARQ global pool to enqueue jobs from FastAPI
async def get_arq_pool():
    global _pool
    if _pool is None:
        _pool = await create_pool(REDIS_SETTINGS)
    return _pool
