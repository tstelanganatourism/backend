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

        finalized_bookings = []
        for draft in drafts:
            logger.info(f"Checking status and releasing expired draft {draft.draft_id} for order {draft.pg_transaction_id}")
            
            try:
                is_paid = False
                gateway_payment_id = None
                
                if draft.pg_transaction_id:
                    if draft.payment_gateway == "PHONEPE":
                        try:
                            from app.services.phonepe_client import phonepe_service
                            check_res = await phonepe_service.get_transaction_status(draft.pg_transaction_id)
                            if check_res.get("status") == "SUCCESS":
                                is_paid = True
                                gateway_payment_id = check_res.get("gateway_payment_id")
                        except Exception as pe_err:
                            logger.error(f"Failed to check PhonePe status for draft {draft.draft_id}: {pe_err}")
                
                if is_paid:
                    logger.info(f"Draft {draft.draft_id} was paid successfully. Finalizing instead of releasing.")
                    from app.api.v1.payments import _finalize_draft
                    public_id = await _finalize_draft(draft, gateway_payment_id or draft.pg_transaction_id, db, payment_source=draft.payment_gateway)
                    finalized_bookings.append((public_id, draft.pg_transaction_id))
                else:
                    from app.api.v1.payments import release_draft_inventory
                    await release_draft_inventory(draft, db)
                    await db.delete(draft)
            except Exception as e:
                logger.error(f"Error processing expired draft {draft.draft_id}: {str(e)}")
                
        await db.commit()
        logger.info(f"Successfully cleaned up {len(drafts)} expired drafts.")

        # Post-commit: Enqueue document tasks for finalized bookings to prevent race conditions
        if finalized_bookings:
            from app.worker import get_arq_pool
            from app.models.booking import Booking
            from app.models.enums import BookingStatus
            
            try:
                arq_pool = await get_arq_pool()
                for public_id, pg_txn_id in finalized_bookings:
                    try:
                        stmt = select(Booking).where(Booking.public_id == public_id)
                        res = await db.execute(stmt)
                        booking = res.scalar_one_or_none()
                        if booking:
                            is_fully_paid = (booking.status == BookingStatus.FULLY_PAID)
                            await arq_pool.enqueue_job("process_post_booking_documents_task", booking.id, is_fully_paid)
                            logger.info(f"Successfully enqueued post-booking documents task from cleanup worker for booking {public_id}")
                        else:
                            logger.warning(f"Booking {public_id} not found in DB after commit for draft PG transaction {pg_txn_id}")
                    except Exception as bk_err:
                        logger.error(f"Failed to enqueue post-booking documents task for {public_id}: {bk_err}")
            except Exception as arq_err:
                logger.error(f"Failed to get arq pool or enqueue jobs: {arq_err}")

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
