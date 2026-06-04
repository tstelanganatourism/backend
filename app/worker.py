import asyncio
import logging
from arq import create_pool
from arq.connections import RedisSettings
from app.core.config import settings
from app.services.pdf_generator import generate_package_brochure_task, process_post_booking_documents_task

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
    pass

class WorkerSettings:
    functions = [generate_package_brochure_task, process_post_booking_documents_task]
    redis_settings = REDIS_SETTINGS
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 2  # Keep concurrency low since Playwright is CPU-heavy
    max_tries = 2 # Fail fast if Playwright rendering timeouts or crashes
    job_timeout = 300  # 5 minutes timeout for generation
    
_pool = None

# ARQ global pool to enqueue jobs from FastAPI
async def get_arq_pool():
    global _pool
    if _pool is None:
        _pool = await create_pool(REDIS_SETTINGS)
    return _pool
