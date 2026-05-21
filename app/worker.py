import asyncio
import logging
from arq import create_pool
from arq.connections import RedisSettings
from app.core.config import settings
from app.services.pdf_generator import generate_package_brochure_task, generate_booking_ticket_task, generate_booking_invoice_task

logger = logging.getLogger(__name__)

# Parse Upstash/Redis connection string
REDIS_SETTINGS = RedisSettings.from_dsn(settings.REDIS_URL)

async def startup(ctx):
    logger.info("Worker starting up...")
    # Initialize any required resources
    pass

async def shutdown(ctx):
    logger.info("Worker shutting down...")
    pass

class WorkerSettings:
    functions = [generate_package_brochure_task, generate_booking_ticket_task, generate_booking_invoice_task]
    redis_settings = REDIS_SETTINGS
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 2  # Keep concurrency low since Playwright is CPU-heavy
    max_tries = 2 # Fail fast if Playwright rendering timeouts or crashes
    job_timeout = 300  # 5 minutes timeout for generation
    
# ARQ global pool to enqueue jobs from FastAPI
async def get_arq_pool():
    return await create_pool(REDIS_SETTINGS)
