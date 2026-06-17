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

    # ── Travel Day SMS Reminders ──────────────────────────────────────────────
    # Send to all confirmed bookings travelling today (idempotent via Redis key)
    try:
        from app.models.booking import Booking, BookingPassenger
        from app.models.enums import BookingStatus as BStatus
        from app.models.user import User as UserModel
        from app.services.sms_service import send_travel_reminder_sms, send_room_reminder_sms

        async with AsyncSessionLocal() as sms_db:
            # Package bookings today
            pkg_res = await sms_db.execute(
                select(Booking)
                .where(
                    Booking.travel_date == today,
                    Booking.status.in_([BStatus.FULLY_PAID, BStatus.PARTIAL_PAID]),
                    Booking.variant_id.isnot(None),
                )
            )
            pkg_bookings = pkg_res.scalars().all()

            for bk in pkg_bookings:
                sms_key = f"travel_sms_sent:{bk.id}:{today.isoformat()}"
                if await redis.exists(sms_key):
                    continue

                # Get phone and name from primary passenger, falling back to any passenger and then the User model
                pax_stmt = select(BookingPassenger.full_name, BookingPassenger.phone_number).where(
                    BookingPassenger.booking_id == bk.id,
                    BookingPassenger.is_primary == True
                ).limit(1)
                pax_res = await sms_db.execute(pax_stmt)
                pax_row = pax_res.first()
                
                phone = None
                cust_full_name = "Customer"
                
                if pax_row:
                    cust_full_name = pax_row[0]
                    phone = pax_row[1]
                    
                if not phone:
                    # Fallback to any passenger with a phone number
                    pax_any_stmt = select(BookingPassenger.full_name, BookingPassenger.phone_number).where(
                        BookingPassenger.booking_id == bk.id,
                        BookingPassenger.phone_number.isnot(None)
                    ).limit(1)
                    pax_any_res = await sms_db.execute(pax_any_stmt)
                    pax_any_row = pax_any_res.first()
                    if pax_any_row:
                        cust_full_name = pax_any_row[0]
                        phone = pax_any_row[1]
                        
                if not phone:
                    # Fallback to user model
                    u_res = await sms_db.execute(select(UserModel).where(UserModel.id == bk.user_id))
                    u = u_res.scalar_one_or_none()
                    phone = getattr(u, "phone_number", None) if u else None
                    if u and u.full_name:
                        cust_full_name = u.full_name
                        
                if not phone:
                    await redis.set(sms_key, "1", ex=86400)
                    continue

                cust_name = (cust_full_name or "Customer").split()[0]
                snap = bk.pricing_snapshot or {}
                pkg_title = snap.get("package_title") or "Boat Tour"
                
                # Extract boarding point details
                bp_snap = snap.get("boarding_point") or {}
                if isinstance(bp_snap, str):
                    boarding_title = bp_snap
                    boarding_time = "7:30 AM"
                    boarding_landmark = "Near SBI ATM"
                    boarding_phone = "9542069573"
                else:
                    boarding_title = bp_snap.get("title") or "Boarding Point"
                    boarding_time = bp_snap.get("departure_time") or "7:30 AM"
                    boarding_landmark = bp_snap.get("landmark") or "Near SBI ATM"
                    boarding_phone = bp_snap.get("contact_number") or "9542069573"

                await send_travel_reminder_sms(
                    customer_name=cust_name,
                    customer_phone=phone,
                    public_id=bk.public_id,
                    package_title=pkg_title,
                    boarding_title=boarding_title,
                    boarding_time=boarding_time,
                    boarding_landmark=boarding_landmark,
                    boarding_phone=boarding_phone,
                )
                await redis.set(sms_key, "1", ex=86400)
                logger.info(f"[TravelSMS] Sent package reminder to {phone} for {bk.public_id}")

            # Room bookings today
            from app.models.room import RoomVariant, Room
            from sqlalchemy.orm import selectinload

            room_res = await sms_db.execute(
                select(Booking)
                .where(
                    Booking.travel_date == today,
                    Booking.status.in_([BStatus.FULLY_PAID, BStatus.PARTIAL_PAID]),
                    Booking.room_variant_id.isnot(None),
                )
                .options(
                    selectinload(Booking.room_variant).selectinload(RoomVariant.room)
                )
            )
            room_bookings = room_res.scalars().all()

            for bk in room_bookings:
                sms_key = f"travel_sms_sent:{bk.id}:{today.isoformat()}"
                if await redis.exists(sms_key):
                    continue

                # Get phone and name from primary passenger, falling back to any passenger and then the User model
                pax_stmt = select(BookingPassenger.full_name, BookingPassenger.phone_number).where(
                    BookingPassenger.booking_id == bk.id,
                    BookingPassenger.is_primary == True
                ).limit(1)
                pax_res = await sms_db.execute(pax_stmt)
                pax_row = pax_res.first()
                
                phone = None
                cust_full_name = "Customer"
                
                if pax_row:
                    cust_full_name = pax_row[0]
                    phone = pax_row[1]
                    
                if not phone:
                    # Fallback to any passenger with a phone number
                    pax_any_stmt = select(BookingPassenger.full_name, BookingPassenger.phone_number).where(
                        BookingPassenger.booking_id == bk.id,
                        BookingPassenger.phone_number.isnot(None)
                    ).limit(1)
                    pax_any_res = await sms_db.execute(pax_any_stmt)
                    pax_any_row = pax_any_res.first()
                    if pax_any_row:
                        cust_full_name = pax_any_row[0]
                        phone = pax_any_row[1]
                        
                if not phone:
                    # Fallback to user model
                    u_res = await sms_db.execute(select(UserModel).where(UserModel.id == bk.user_id))
                    u = u_res.scalar_one_or_none()
                    phone = getattr(u, "phone_number", None) if u else None
                    if u and u.full_name:
                        cust_full_name = u.full_name
                        
                if not phone:
                    await redis.set(sms_key, "1", ex=86400)
                    continue

                cust_name = (cust_full_name or "Customer").split()[0]
                
                # Fetch lodge and checkin details
                r_var = bk.room_variant
                r_room = r_var.room if r_var else None
                
                if r_room:
                    lodge_name = r_room.lodge_name or "Lodge Stay"
                    checkin_time = r_room.slot_start.strftime("%I:%M %p") if r_room.slot_start else "11:00 AM"
                else:
                    snap = bk.pricing_snapshot or {}
                    lodge_name = snap.get("room_title") or snap.get("room_name") or "Lodge Stay"
                    checkin_time = "11:00 AM"

                checkin_date_str = bk.travel_date.strftime("%d-%b-%Y")
                checkin_detail = f"{checkin_date_str} at {checkin_time}"

                await send_room_reminder_sms(
                    customer_name=cust_name,
                    customer_phone=phone,
                    public_id=bk.public_id,
                    lodge_name=lodge_name,
                    checkin_detail=checkin_detail,
                )
                await redis.set(sms_key, "1", ex=86400)
                logger.info(f"[TravelSMS] Sent room reminder to {phone} for {bk.public_id}")

    except Exception as sms_err:
        logger.error(f"[TravelSMS] Error sending travel reminders: {sms_err}")
