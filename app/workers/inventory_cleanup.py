"""
Inventory Cleanup Cron Job

ARQ-compatible wrapper around release_expired_drafts.
Runs every 5 minutes to release inventory held by expired/unpaid booking drafts.

Without this, seats reserved by customers who started checkout but never paid
would remain locked forever, silently reducing available capacity.
"""

from loguru import logger


async def cleanup_expired_drafts(ctx):
    """
    ARQ cron task: Release inventory held by expired booking drafts.
    Wraps the standalone cleanup_worker logic into the ARQ cron system.
    """
    try:
        from app.workers.cleanup_worker import release_expired_drafts
        await release_expired_drafts()
    except Exception as e:
        logger.error(f"[CleanupCron] Unexpected error during expired draft cleanup: {e}")
