"""
Daily Cutoff Worker

Closes all PackageVariantInventory rows for the current date if the time in IST is >= 6:00 AM.
Ensures that tourists cannot book same-day packages after 6 AM IST.
Uses Redis to ensure it only runs once per day, allowing admins to manually override
(open) the inventory from the dashboard later in the day if they wish.
"""

import asyncio
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.session import AsyncSessionLocal
from app.models.package import PackageVariantInventory, PackageVariant
from app.services.redis_client import get_redis
from app.core.timezone import get_ist_now

CUTOFF_CHECK_INTERVAL_SECONDS = 300

async def perform_daily_cutoff(ctx):
    now_ist = get_ist_now()
    today = now_ist.date()
    
    if now_ist.hour < 6:
        return
        
    redis = get_redis()
    key = f"daily_cutoff_done:{today.isoformat()}"
    
    already_done = await redis.get(key)
    if already_done:
        return
        
    async with AsyncSessionLocal() as db:
        query = (
            select(PackageVariantInventory)
            .where(
                PackageVariantInventory.date == today,
                PackageVariantInventory.is_closed == False
            )
        )
        result = await db.execute(query)
        rows_to_close = result.scalars().all()
        
        # Room inventory cutoff
        from app.models.room import RoomSlotInventory, RoomVariant
        room_query = (
            select(RoomSlotInventory)
            .where(
                RoomSlotInventory.date == today,
                RoomSlotInventory.is_closed == False
            )
        )
        room_result = await db.execute(room_query)
        room_rows_to_close = room_result.scalars().all()
        
        if not rows_to_close and not room_rows_to_close:
            await redis.set(key, "1", ex=86400)
            return
            
        logger.info(f"[DailyCutoff] Closing {len(rows_to_close)} package rows and {len(room_rows_to_close)} room rows for {today} at {now_ist.time()}")
        
        for row in rows_to_close:
            row.is_closed = True
            
        for r_row in room_rows_to_close:
            r_row.is_closed = True
            
        await db.commit()
        
        # Clear redis cache to ensure frontend sees the updated closed state
        from app.utils.cache import clear_cache_prefix
        clear_cache_prefix("inventory:packages:")
        clear_cache_prefix("packages:list:")
        clear_cache_prefix("rooms:list:")
        
        # Broadcast SSE updates
        from app.utils.sse import sse_manager
        import time as builtin_time
        
        if rows_to_close:
            # Re-fetch with variant and package to send SSE and clear specific caches
            query = (
                select(PackageVariantInventory)
                .options(selectinload(PackageVariantInventory.variant).selectinload(PackageVariant.package))
                .where(PackageVariantInventory.id.in_([r.id for r in rows_to_close]))
            )
            res = await db.execute(query)
            closed_invs = res.scalars().all()
            
            for inv in closed_invs:
                variant = inv.variant
                clear_cache_prefix(f"packages:detail:{variant.package.slug}")
                from app.api.v1.public_packages import get_effective_package_prices
                eff_adult, eff_child = get_effective_package_prices(variant.adult_price, variant.child_price, inv.price_override)
                p = {
                    "version": int(builtin_time.time() * 1000),
                    "timestamp": now_ist.isoformat(),
                    "package_id": variant.package_id,
                    "travel_date": str(today),
                    "available": inv.total_capacity - (inv.booked_count + inv.reserved_count),
                    "reserved": inv.reserved_count,
                    "booked": inv.booked_count,
                    "is_closed": True,
                    "effective_adult_price": float(eff_adult),
                    "effective_child_price": float(eff_child),
                    "variant_id": variant.id
                }
                await sse_manager.broadcast_event("package", str(variant.package_id), "INVENTORY_UPDATE", p)
                
        if room_rows_to_close:
            room_q = (
                select(RoomSlotInventory)
                .options(selectinload(RoomSlotInventory.room_variant).selectinload(RoomVariant.room))
                .where(RoomSlotInventory.id.in_([r.id for r in room_rows_to_close]))
            )
            r_res = await db.execute(room_q)
            closed_room_invs = r_res.scalars().all()
            
            for inv in closed_room_invs:
                variant = inv.room_variant
                clear_cache_prefix(f"rooms:detail:{variant.room.slug}")
                p = {
                    "version": int(builtin_time.time() * 1000),
                    "timestamp": now_ist.isoformat(),
                    "room_id": variant.room_id,
                    "travel_date": str(today),
                    "available": inv.total_rooms - (inv.booked_rooms + inv.reserved_rooms),
                    "reserved": inv.reserved_rooms,
                    "booked": inv.booked_rooms,
                    "is_closed": True,
                    "variant_id": variant.id,
                    "slot_start": str(inv.slot_start),
                    "slot_end": str(inv.slot_end)
                }
                await sse_manager.broadcast_event("rooms", str(variant.room_id), "INVENTORY_UPDATE", p)
            
        await redis.set(key, "1", ex=86400)

