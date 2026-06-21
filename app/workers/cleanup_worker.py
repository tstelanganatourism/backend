import asyncio
import os
import sys
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from loguru import logger

# Ensure backend root is in PYTHONPATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.db.session import AsyncSessionLocal
from app.models.booking import BookingDraft
from app.models.package import PackageVariantInventory
from app.models.room import RoomSlotInventory
from app.core.timezone import get_ist_now

async def release_expired_drafts():
    """
    Finds expired BookingDrafts, releases their reserved inventory, and deletes them.
    """
    logger.info("Running expired drafts cleanup worker...")
    async with AsyncSessionLocal() as db:
        now = get_ist_now()
        
        # Find all expired drafts
        query = select(BookingDraft).where(BookingDraft.expires_at < now).with_for_update()
        result = await db.execute(query)
        drafts = result.scalars().all()
        
        if not drafts:
            logger.info("No expired drafts found.")
            return

        for draft in drafts:
            logger.info(f"Releasing expired draft {draft.draft_id} for order {draft.pg_transaction_id}")
            
            try:
                from app.api.v1.payments import release_draft_inventory
                await release_draft_inventory(draft, db)
                await db.delete(draft)
            except Exception as e:
                logger.error(f"Error releasing draft {draft.draft_id}: {str(e)}")
                
        await db.commit()
        logger.info(f"Successfully cleaned up {len(drafts)} expired drafts.")

async def run_worker():
    while True:
        try:
            await release_expired_drafts()
        except Exception as e:
            logger.error(f"Cleanup worker failed: {str(e)}")
        # Run every 5 minutes
        await asyncio.sleep(300)

if __name__ == "__main__":
    logger.add("cleanup_worker.log", rotation="10 MB")
    asyncio.run(run_worker())
