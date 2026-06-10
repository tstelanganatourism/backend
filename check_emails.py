import asyncio
from app.db.session import AsyncSessionLocal
from sqlalchemy import select
from app.models.booking import Booking, EmailLog
from app.services.pdf_generator import process_post_booking_documents_task

async def run():
    async with AsyncSessionLocal() as db:
        # Find all booking IDs that have failed emails, or no emails sent at all
        # Let's get all bookings that don't have a SENT email log
        stmt = select(Booking.id).where(
            ~Booking.id.in_(
                select(EmailLog.booking_id).where(EmailLog.delivery_status == 'SENT')
            )
        )
        res = await db.execute(stmt)
        booking_ids_to_retry = res.scalars().all()
        
        # Also find any explicitly FAILED ones just in case
        stmt_failed = select(EmailLog.booking_id).where(EmailLog.delivery_status == 'FAILED')
        res_failed = await db.execute(stmt_failed)
        failed_ids = res_failed.scalars().all()
        
        all_ids = set(booking_ids_to_retry) | set(failed_ids)
        
        print(f"Found {len(all_ids)} bookings with pending/failed emails.")
        
    # We close the DB session here to prevent holding connections
    
    # Process them sequentially to avoid connection pool exhaustion
    for idx, b_id in enumerate(all_ids, 1):
        print(f"[{idx}/{len(all_ids)}] Processing emails for booking ID: {b_id}")
        try:
            await process_post_booking_documents_task(None, b_id)
            print(f"Successfully processed {b_id}")
        except Exception as e:
            print(f"Failed to process {b_id}: {e}")

if __name__ == "__main__":
    asyncio.run(run())
