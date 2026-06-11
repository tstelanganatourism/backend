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
                if draft.target_type == 'package':
                    inv_query = select(PackageVariantInventory).where(
                        PackageVariantInventory.variant_id == draft.variant_id,
                        PackageVariantInventory.date == draft.travel_date
                    ).with_for_update()
                    inv_res = await db.execute(inv_query)
                    inv = inv_res.scalar_one_or_none()
                    if inv:
                        inv.reserved_count = max(0, inv.reserved_count - draft.quantity)

                elif draft.target_type == 'room':
                    # Parse stay dates from payload
                    payload = draft.checkout_payload
                    from datetime import date, timedelta, time
                    arrival = date.fromisoformat(payload['travel_date'])
                    departure_str = payload.get('departure_date')
                    departure = date.fromisoformat(departure_str) if departure_str else (arrival + timedelta(days=1))
                    
                    current = arrival
                    stay_dates = []
                    while current < departure:
                        stay_dates.append(current)
                        current += timedelta(days=1)
                        
                    slot_start = time.fromisoformat(payload['slot_start']) if payload.get('slot_start') else None
                    slot_end = time.fromisoformat(payload['slot_end']) if payload.get('slot_end') else None

                    # Required rooms logic
                    from app.models.room import RoomVariant
                    room_var = await db.execute(select(RoomVariant).where(RoomVariant.id == draft.room_variant_id))
                    rv = room_var.scalar_one_or_none()
                    if rv:
                        from app.services.room_calculation import calculate_required_rooms
                        required_rooms = calculate_required_rooms(draft.quantity, rv.capacity_per_room)

                        for stay_date in stay_dates:
                            inv_query = select(RoomSlotInventory).where(
                                RoomSlotInventory.room_variant_id == draft.room_variant_id,
                                RoomSlotInventory.date == stay_date,
                                RoomSlotInventory.slot_start == slot_start,
                                RoomSlotInventory.slot_end == slot_end
                            ).with_for_update()
                            inv_res = await db.execute(inv_query)
                            room_inv = inv_res.scalar_one_or_none()
                            if room_inv:
                                room_inv.reserved_rooms = max(0, room_inv.reserved_rooms - required_rooms)
                
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
