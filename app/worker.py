import asyncio
import logging
from arq import create_pool, cron
from arq.connections import RedisSettings
from app.core.config import settings
from app.services.pdf_generator import generate_package_brochure_task, process_post_booking_documents_task
from app.workers.daily_cutoff import perform_daily_cutoff

logger = logging.getLogger(__name__)

# Parse Upstash/Redis connection string with high timeout for secure remote TLS
REDIS_SETTINGS = RedisSettings.from_dsn(settings.REDIS_URL)
REDIS_SETTINGS.conn_timeout = 15
REDIS_SETTINGS.conn_retries = 10
REDIS_SETTINGS.conn_retry_delay = 2


async def startup(ctx):
    logger.info("Worker starting up...")
    # Initialize any required resources
    pass

async def shutdown(ctx):
    logger.info("Worker shutting down...")
    from app.db.session import engine
    await engine.dispose()
    logger.info("Database connection pool disposed gracefully.")

class WorkerSettings:
    functions = [generate_package_brochure_task, process_post_booking_documents_task]
    cron_jobs = [
        cron(perform_daily_cutoff, second=0, run_at_startup=True),
    ]
    redis_settings = REDIS_SETTINGS
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 3   # Must not exceed the ARQ worker's DB pool ceiling (pool_size=2, max_overflow=1)
    max_tries = 3  # Retry up to 3 times if email sending fails
    job_timeout = 90   # 90 seconds to handle slow Brevo or DB responses
    
_pool = None

# ARQ global pool to enqueue jobs from FastAPI
async def get_arq_pool():
    global _pool
    if _pool is None:
        _pool = await create_pool(REDIS_SETTINGS)
    return _pool
