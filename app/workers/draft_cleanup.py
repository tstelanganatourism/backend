"""
Draft Cleanup Worker — Phase 1

Scans for expired BookingDraft rows and:
  1. Releases reserved_count / reserved_rooms back to inventory.
  2. Deletes the expired draft.

Idempotency guarantees:
  - Uses SELECT FOR UPDATE SKIP LOCKED so multiple concurrent calls
    never process the same draft row twice.
  - Before releasing inventory, checks whether the webhook already
    converted the draft to a confirmed Booking. If it has, inventory
    was already promoted (reserved → booked), so we skip the release
    and only delete the stale draft record.
  - `max(0, ...)` clamps every decrement so reserved counts can never
    go below zero even if the worker runs unexpectedly twice.

Business rule clarity (reconciled with codebase):
  - Agents pay their full reduced payable (total_amount − commission)
    in a single Razorpay transaction. There is no partial-payment path.
  - custom_payment_amount has been removed from CheckoutRequest.
"""

import asyncio
from datetime import datetime, timezone, date, timedelta, time
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.booking import BookingDraft, Booking
from app.models.package import PackageVariantInventory
from app.models.room import RoomSlotInventory, RoomVariant

# ── Configuration ─────────────────────────────────────────────────────────────
CLEANUP_INTERVAL_SECONDS = 300  # Run every 5 minutes


# ── Inventory release helper ──────────────────────────────────────────────────

async def release_draft_inventory(draft: BookingDraft, db: AsyncSession) -> list:
    """
    Release the reserved inventory that was locked when this draft was created.
    """
    sse_payloads = []
    if draft.target_type == "package" and draft.variant_id:
        inv_result = await db.execute(
            select(PackageVariantInventory)
            .where(
                PackageVariantInventory.variant_id == draft.variant_id,
                PackageVariantInventory.date == draft.travel_date,
            )
            .with_for_update()
        )
        inventory = inv_result.scalar_one_or_none()
        if inventory:
            released = min(draft.quantity, inventory.reserved_count)
            inventory.reserved_count = max(0, inventory.reserved_count - draft.quantity)
            logger.info(
                f"[DraftCleanup] Package inventory released: "
                f"variant={draft.variant_id} date={draft.travel_date} qty={released}"
            )
            await db.flush()
            
            import time as builtin_time
            from app.core.timezone import get_ist_now
            from app.models.package import PackageVariant
            v_res = await db.execute(select(PackageVariant).where(PackageVariant.id == draft.variant_id))
            variant = v_res.scalar_one_or_none()
            if variant:
                from app.api.v1.public_packages import get_effective_package_prices
                eff_adult, eff_child = get_effective_package_prices(variant.adult_price, variant.child_price, inventory.price_override)
                sse_payloads.append({
                    "version": int(builtin_time.time() * 1000),
                    "timestamp": get_ist_now().isoformat(),
                    "package_id": variant.package_id,
                    "travel_date": str(draft.travel_date),
                    "available": inventory.total_capacity - (inventory.booked_count + inventory.reserved_count),
                    "reserved": inventory.reserved_count,
                    "booked": inventory.booked_count,
                    "is_closed": inventory.is_closed,
                    "effective_adult_price": float(eff_adult),
                    "effective_child_price": float(eff_child),
                    "variant_id": draft.variant_id
                })

    elif draft.target_type == "room" and draft.room_variant_id:
        payload = draft.checkout_payload or {}

        # Reconstruct stay-date range from stored payload
        arrival = (
            date.fromisoformat(str(payload["travel_date"]))
            if "travel_date" in payload
            else draft.travel_date
        )
        departure_str = payload.get("departure_date")
        departure = (
            date.fromisoformat(departure_str)
            if departure_str
            else arrival + timedelta(days=1)
        )

        current = arrival
        stay_dates: list[date] = []
        while current < departure:
            stay_dates.append(current)
            current += timedelta(days=1)

        slot_start_raw = payload.get("slot_start")
        slot_end_raw = payload.get("slot_end")
        slot_start = time.fromisoformat(slot_start_raw) if slot_start_raw else None
        slot_end = time.fromisoformat(slot_end_raw) if slot_end_raw else None

        # Fetch room variant for capacity_per_room
        rv_result = await db.execute(
            select(RoomVariant).where(RoomVariant.id == draft.room_variant_id)
        )
        rv = rv_result.scalar_one_or_none()
        if not rv:
            logger.warning(
                f"[DraftCleanup] RoomVariant {draft.room_variant_id} not found; "
                f"cannot release room inventory for draft {draft.draft_id}"
            )
            return

        from app.services.room_calculation import calculate_required_rooms
        required_rooms = calculate_required_rooms(draft.quantity, rv.capacity_per_room)

        for stay_date in stay_dates:
            inv_result = await db.execute(
                select(RoomSlotInventory)
                .where(
                    RoomSlotInventory.room_variant_id == draft.room_variant_id,
                    RoomSlotInventory.date == stay_date,
                    RoomSlotInventory.slot_start == slot_start,
                    RoomSlotInventory.slot_end == slot_end,
                )
                .with_for_update()
            )
            inv = inv_result.scalar_one_or_none()
            if inv:
                inv.reserved_rooms = max(0, inv.reserved_rooms - required_rooms)
                logger.info(
                    f"[DraftCleanup] Room inventory released: "
                    f"room_variant={draft.room_variant_id} date={stay_date} rooms={required_rooms}"
                )

    return sse_payloads


# ── Main cleanup coroutine ────────────────────────────────────────────────────

async def cleanup_expired_drafts() -> int:
    """
    Single cleanup sweep.

    Returns the number of draft rows processed (released + deleted).

    Race-condition safety:
      ─ Uses SKIP LOCKED so concurrent calls (or a webhook) holding a
        row-level lock are silently skipped and re-visited next cycle.
      ─ Checks for an existing Booking before touching inventory, to
        handle the case where the webhook finalized the draft just after
        the worker's initial scan.
    """
    now = datetime.now(timezone.utc)
    processed = 0

    async with AsyncSessionLocal() as db:
        # 1. Bulk scan — no row lock yet, just IDs
        scan_result = await db.execute(
            select(BookingDraft.id).where(BookingDraft.expires_at < now)
        )
        expired_ids: list[int] = [row[0] for row in scan_result.all()]

        for draft_id in expired_ids:
            try:
                # 2. Re-fetch with row-level lock; SKIP LOCKED means we ignore
                #    rows currently held by the webhook transaction.
                locked_result = await db.execute(
                    select(BookingDraft)
                    .where(BookingDraft.id == draft_id)
                    .with_for_update(skip_locked=True)
                )
                draft = locked_result.scalar_one_or_none()

                if draft is None:
                    # Either deleted by webhook, or locked by another transaction.
                    continue

                # 3. Idempotency: did the webhook already convert this draft?
                already_booked = False
                if draft.razorpay_order_id:
                    booking_check = await db.execute(
                        select(Booking.id).where(
                            Booking.pricing_snapshot["razorpay_order_id"].astext
                            == draft.razorpay_order_id
                        )
                    )
                    already_booked = booking_check.scalar_one_or_none() is not None

                if already_booked:
                    # Webhook already promoted reserved → booked; only clean up draft.
                    await db.delete(draft)
                    await db.commit()
                    logger.info(
                        f"[DraftCleanup] Draft {draft.draft_id} was already finalized "
                        f"by webhook — stale draft record removed."
                    )
                else:
                    # Normal expiry: release inventory then delete.
                    sse_payloads = await release_draft_inventory(draft, db)
                    await db.delete(draft)
                    await db.commit()
                    
                    from app.utils.sse import sse_manager
                    for p in sse_payloads:
                        await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)
                    logger.info(
                        f"[DraftCleanup] Expired draft {draft.draft_id} cleaned: "
                        f"type={draft.target_type} qty={draft.quantity} "
                        f"travel={draft.travel_date}"
                    )

                processed += 1

            except Exception as exc:
                logger.error(
                    f"[DraftCleanup] Error processing draft id={draft_id}: {exc}"
                )
                await db.rollback()

    return processed


# ── Background loop ───────────────────────────────────────────────────────────

async def draft_cleanup_loop() -> None:
    """
    Infinite-loop coroutine. Started once by FastAPI's startup event.
    Runs cleanup_expired_drafts every CLEANUP_INTERVAL_SECONDS seconds.
    """
    logger.info(
        f"[DraftCleanup] Worker started. "
        f"Sweep interval: {CLEANUP_INTERVAL_SECONDS}s ({CLEANUP_INTERVAL_SECONDS // 60} min)."
    )
    # Small initial delay so the DB connection pool warms up first.
    await asyncio.sleep(30)

    while True:
        try:
            count = await cleanup_expired_drafts()
            if count > 0:
                logger.info(
                    f"[DraftCleanup] Sweep complete — {count} expired draft(s) processed."
                )
            else:
                logger.debug("[DraftCleanup] Sweep complete — no expired drafts.")
        except Exception as exc:
            logger.error(f"[DraftCleanup] Unexpected error in sweep loop: {exc}")

        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
