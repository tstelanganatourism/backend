"""
Admin Inventory Router — Phase 3.3

Manages per-date capacity, open/close status, and price overrides
for PackageVariantInventory rows.

All routes are admin-only.

Routes:
  POST   /api/v1/admin/inventory/packages/generate
  GET    /api/v1/admin/inventory/packages/{variant_id}
  GET    /api/v1/admin/inventory/packages/{variant_id}/calendar
  PATCH  /api/v1/admin/inventory/packages/{variant_id}/{date}
  DELETE /api/v1/admin/inventory/packages/{variant_id}/{date}
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import ist_date_today
from app.db.session import get_db
from app.middleware.auth import require_admin
from app.models.package import Package, PackageVariant, PackageVariantInventory
from app.models.user import User
from app.schemas.inventory import (
    PackageInventoryGenerateRequest,
    PackageInventoryGenerateResponse,
    PackageInventoryRow,
    PackageInventoryUpdateRequest,
)
from app.utils.audit import log_action
from app.utils.cache import clear_cache_prefix, ttl_cache_get_or_set

router = APIRouter(
    prefix="/inventory",
    tags=["Admin - Inventory"],
    dependencies=[Depends(require_admin)],
)

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _compute_row(row: PackageVariantInventory) -> PackageInventoryRow:
    return PackageInventoryRow(
        id=row.id,
        variant_id=row.variant_id,
        date=row.date,
        total_capacity=row.total_capacity,
        booked_count=row.booked_count,
        available_seats=max(0, row.total_capacity - row.booked_count - row.reserved_count),
        is_closed=row.is_closed,
        price_override=row.price_override,
    )


async def _clear_package_cache_for_variant(db: AsyncSession, variant_id: int) -> None:
    import asyncio
    result = await db.execute(
        select(Package.id, Package.slug).join(PackageVariant, PackageVariant.package_id == Package.id).where(
            PackageVariant.id == variant_id
        )
    )
    row = result.first()
    if row:
        pkg_id, slug = row
        clear_cache_prefix("packages:list:")
        clear_cache_prefix(f"packages:detail:{slug}")
        # Fire-and-forget: Redis SCAN can be slow, don't block the response
        from app.services.redis_client import invalidate_cached_availability
        asyncio.create_task(invalidate_cached_availability(slug))
        from app.utils.cache import trigger_frontend_revalidation
        trigger_frontend_revalidation(tags=[f"package-{pkg_id}"])


# ─── Generate inventory rows ──────────────────────────────────────────────────

@router.post(
    "/packages/generate",
    response_model=PackageInventoryGenerateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_package_inventory(
    body: PackageInventoryGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """
    Generate inventory rows for a package variant over a date range.
    Skips dates that already have a row. Allows up to 365-day ranges.
    """
    today = ist_date_today()

    # Validate that variant exists
    variant_result = await db.execute(
        select(PackageVariant).where(PackageVariant.id == body.variant_id)
    )
    variant = variant_result.scalar_one_or_none()
    if not variant:
        raise HTTPException(status_code=404, detail="Package variant not found.")

    # Date range validation
    if body.to_date < body.from_date:
        raise HTTPException(status_code=400, detail="to_date must be >= from_date.")
    if (body.to_date - body.from_date).days > 365:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 365 days.")

    # Fetch existing rows to skip duplicates
    existing_result = await db.execute(
        select(PackageVariantInventory.date).where(
            and_(
                PackageVariantInventory.variant_id == body.variant_id,
                PackageVariantInventory.date >= body.from_date,
                PackageVariantInventory.date <= body.to_date,
            )
        )
    )
    existing_dates = {row for (row,) in existing_result.all()}

    created = 0
    skipped = 0
    current = body.from_date

    while current <= body.to_date:
        if current in existing_dates:
            skipped += 1
        else:
            row = PackageVariantInventory(
                variant_id=body.variant_id,
                date=current,
                total_capacity=body.total_capacity,
                booked_count=0,
                is_closed=False,
                price_override=None,
            )
            db.add(row)
            created += 1
        current += timedelta(days=1)

    await db.commit()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="GENERATE_INVENTORY",
        entity_type="PackageVariant",
        entity_id=str(body.variant_id),
        details={
            "from_date": str(body.from_date),
            "to_date": str(body.to_date),
            "total_capacity": body.total_capacity,
            "created": created,
            "skipped": skipped,
        },
    )
    await db.commit()
    clear_cache_prefix(f"inventory:packages:{body.variant_id}")
    await _clear_package_cache_for_variant(db, body.variant_id)

    # Broadcast SSE for newly created inventory dates
    if created > 0:
        from app.utils.sse import sse_manager, build_package_sse_payload
        from sqlalchemy.orm import joinedload
        variant_res = await db.execute(
            select(PackageVariant)
            .options(joinedload(PackageVariant.package))
            .where(PackageVariant.id == body.variant_id)
        )
        variant = variant_res.scalar_one_or_none()
        if variant:
            current_date = body.from_date
            while current_date <= body.to_date:
                if current_date not in existing_dates:
                    sse_payload = build_package_sse_payload(variant, None, current_date)
                    sse_payload["available"] = body.total_capacity
                    await sse_manager.broadcast_event("package", str(variant.package_id), "INVENTORY_UPDATE", sse_payload)
                current_date += timedelta(days=1)

    return PackageInventoryGenerateResponse(
        created=created,
        skipped=skipped,
        message=f"Generated {created} inventory rows, skipped {skipped} existing.",
    )


# ─── List inventory rows for a variant ───────────────────────────────────────

@router.get(
    "/packages/{variant_id}",
    response_model=List[PackageInventoryRow],
)
async def list_variant_inventory(
    variant_id: int,
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List all inventory rows for a package variant, optionally filtered by date range."""
    query = select(PackageVariantInventory).where(
        PackageVariantInventory.variant_id == variant_id
    )
    if from_date:
        query = query.where(PackageVariantInventory.date >= from_date)
    if to_date:
        query = query.where(PackageVariantInventory.date <= to_date)

    query = query.order_by(PackageVariantInventory.date.asc())
    result = await db.execute(query)
    rows = result.scalars().all()
    return [_compute_row(r) for r in rows]


# ─── Calendar view ────────────────────────────────────────────────────────────

@router.get(
    "/packages/{variant_id}/calendar",
    response_model=List[PackageInventoryRow],
)
async def get_variant_calendar(
    variant_id: int,
    month: str = Query(..., description="YYYY-MM format"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all inventory rows for a variant within a specific month.
    Used to render the admin calendar grid.
    """
    try:
        year, mon = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format.")

    from_date = date(year, mon, 1)
    # Last day of the month
    if mon == 12:
        to_date = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        to_date = date(year, mon + 1, 1) - timedelta(days=1)

    cache_key = f"inventory:packages:{variant_id}:{month}"

    async def _fetch():
        query = (
            select(PackageVariantInventory)
            .where(
                and_(
                    PackageVariantInventory.variant_id == variant_id,
                    PackageVariantInventory.date >= from_date,
                    PackageVariantInventory.date <= to_date,
                )
            )
            .order_by(PackageVariantInventory.date.asc())
        )
        result = await db.execute(query)
        rows = result.scalars().all()
        return [_compute_row(r) for r in rows]

    return await ttl_cache_get_or_set(cache_key, 60, _fetch)


# ─── Update a single date ─────────────────────────────────────────────────────

@router.patch(
    "/packages/{variant_id}/{inv_date}",
    response_model=PackageInventoryRow,
)
async def update_inventory_row(
    variant_id: int,
    inv_date: date,
    body: PackageInventoryUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """
    Update capacity, close status, or price override for a specific date.
    The date must already exist (generated first).
    """
    today = ist_date_today()
    if inv_date < today:
        raise HTTPException(
            status_code=400,
            detail="Cannot modify inventory for past dates."
        )

    result = await db.execute(
        select(PackageVariantInventory).where(
            and_(
                PackageVariantInventory.variant_id == variant_id,
                PackageVariantInventory.date == inv_date,
            )
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No inventory row found for variant {variant_id} on {inv_date}. Generate it first."
        )

    updates = body.model_dump(exclude_unset=True)

    # Capacity safety: can't reduce below booked_count
    if "total_capacity" in updates and updates["total_capacity"] < row.booked_count:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot reduce capacity to {updates['total_capacity']} "
                f"— {row.booked_count} seats already booked on this date."
            ),
        )

    for key, value in updates.items():
        setattr(row, key, value)

    await db.commit()
    await db.refresh(row)
    
    # Broadcast SSE for Admin Inventory Edit
    from app.utils.sse import sse_manager, build_package_sse_payload
    from sqlalchemy.orm import joinedload
    variant_res = await db.execute(
        select(PackageVariant)
        .options(joinedload(PackageVariant.package))
        .where(PackageVariant.id == variant_id)
    )
    variant = variant_res.scalar_one_or_none()
    if variant:
        sse_payload = build_package_sse_payload(variant, row, inv_date)
        await sse_manager.broadcast_event("package", str(variant.package_id), "INVENTORY_UPDATE", sse_payload)

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="UPDATE_INVENTORY",
        entity_type="PackageVariantInventory",
        entity_id=str(row.id),
        details={"date": str(inv_date), "variant_id": variant_id, **updates},
    )
    await db.commit()
    clear_cache_prefix(f"inventory:packages:{variant_id}")
    await _clear_package_cache_for_variant(db, variant_id)

    return _compute_row(row)


# ─── Delete a single date row ─────────────────────────────────────────────────

@router.delete(
    "/packages/{variant_id}/{inv_date}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_inventory_row(
    variant_id: int,
    inv_date: date,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Delete a specific inventory row. Fails if there are already booked seats."""
    result = await db.execute(
        select(PackageVariantInventory).where(
            and_(
                PackageVariantInventory.variant_id == variant_id,
                PackageVariantInventory.date == inv_date,
            )
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Inventory row not found.")

    if row.booked_count > 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot delete: {row.booked_count} seats already booked on {inv_date}. "
                "Close the date instead."
            ),
        )

    await db.delete(row)
    await db.commit()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="DELETE_INVENTORY",
        entity_type="PackageVariantInventory",
        entity_id=str(row.id),
        details={"date": str(inv_date), "variant_id": variant_id},
    )
    await db.commit()
    
    # Broadcast SSE for Admin Inventory Delete
    from sqlalchemy.orm import joinedload
    variant_res = await db.execute(select(PackageVariant).options(joinedload(PackageVariant.package)).where(PackageVariant.id == variant_id))
    variant = variant_res.scalar_one_or_none()
    if variant:
        from app.utils.sse import sse_manager, build_package_sse_payload
        sse_payload = build_package_sse_payload(variant, None, inv_date)
        sse_payload["available"] = 0
        sse_payload["is_closed"] = True
        await sse_manager.broadcast_event("package", str(variant.package_id), "INVENTORY_UPDATE", sse_payload)

    clear_cache_prefix(f"inventory:packages:{variant_id}")
    await _clear_package_cache_for_variant(db, variant_id)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# ROOM INVENTORY ROUTES  —  Single Source of Truth: RoomSlotInventory per variant
# ═══════════════════════════════════════════════════════════════════════════════

from app.models.room import Room, RoomVariant, RoomSlotInventory
from app.schemas.inventory import (
    RoomInventoryGenerateRequest,
    RoomInventoryGenerateResponse,
    RoomInventoryRow,
    RoomInventoryUpdateRequest,
)


def _compute_room_row(row: RoomSlotInventory) -> RoomInventoryRow:
    return RoomInventoryRow(
        id=row.id,
        room_variant_id=row.room_variant_id,
        date=row.date,
        slot_start=str(row.slot_start),
        slot_end=str(row.slot_end),
        total_rooms=row.total_rooms,
        booked_rooms=row.booked_rooms,
        available_rooms=max(0, row.total_rooms - row.booked_rooms - row.reserved_rooms),
        is_closed=row.is_closed,
    )


# ─── Generate room variant inventory rows ────────────────────────────────────

@router.post(
    "/rooms/generate",
    response_model=RoomInventoryGenerateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_room_inventory(
    body: RoomInventoryGenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """
    Generate RoomSlotInventory rows for a specific room variant over a date range.

    Capacity per date defaults to RoomVariant.total_rooms (the template value set by
    the admin on the Hotel/Rooms page). The admin can pass override_total_rooms to set
    a different daily capacity for this generation run only.

    Skips dates that already have a row (idempotent).
    """
    today = ist_date_today()

    variant_result = await db.execute(
        select(RoomVariant)
        .join(Room, Room.id == RoomVariant.room_id)
        .where(
            RoomVariant.id == body.room_variant_id,
            RoomVariant.deleted_at.is_(None),
            Room.deleted_at.is_(None),
        )
    )
    variant = variant_result.scalar_one_or_none()
    if not variant:
        raise HTTPException(status_code=404, detail="Room variant not found.")

    if variant.total_rooms == 0 and body.override_total_rooms is None and not body.slot_capacities:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Room variant '{variant.variant_name}' has total_rooms=0. "
                "Set a total_rooms value on the variant first, or pass override_total_rooms/slot_capacities."
            )
        )

    if body.to_date < body.from_date:
        raise HTTPException(status_code=400, detail="to_date must be >= from_date.")
    if (body.to_date - body.from_date).days > 365:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 365 days.")

    # Inherit slot_start/slot_end from parent lodge
    room_result = await db.execute(
        select(Room).where(Room.id == variant.room_id, Room.deleted_at.is_(None))
    )
    room = room_result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="Parent room/lodge not found.")

    from datetime import time

    def parse_time(t_str: str) -> time:
        parts = [int(p) for p in t_str.split(':')]
        if len(parts) == 2:
            return time(parts[0], parts[1])
        if len(parts) >= 3:
            return time(parts[0], parts[1], parts[2])
        raise ValueError()

    # Always add the primary slot if present
    slots_to_generate = []
    if room.slot_start is not None and room.slot_end is not None:
        slots_to_generate.append((room.slot_start, room.slot_end))

    # Parse room.booking_slots if present and append them
    if room.booking_slots and isinstance(room.booking_slots, list):
        for slot in room.booking_slots:
            if isinstance(slot, dict) and "slot_start" in slot and "slot_end" in slot:
                try:
                    start_t = parse_time(slot["slot_start"])
                    end_t = parse_time(slot["slot_end"])
                    if (start_t, end_t) not in slots_to_generate:
                        slots_to_generate.append((start_t, end_t))
                except Exception:
                    pass

    if not slots_to_generate:
        raise HTTPException(
            status_code=400,
            detail="No booking slots are configured for this lodge.",
        )

    default_total_rooms = (
        body.override_total_rooms if body.override_total_rooms is not None
        else variant.total_rooms
    )

    slot_capacity_map = {}
    if body.slot_capacities:
        configured_slots = set(slots_to_generate)
        for slot_capacity in body.slot_capacities:
            try:
                key = (parse_time(slot_capacity.slot_start), parse_time(slot_capacity.slot_end))
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid slot time: {slot_capacity.slot_start}-{slot_capacity.slot_end}.",
                )

            if key not in configured_slots:
                raise HTTPException(
                    status_code=400,
                    detail=f"Slot {slot_capacity.slot_start}-{slot_capacity.slot_end} is not configured for this lodge.",
                )

            slot_capacity_map[key] = slot_capacity.total_rooms

    # Query existing (date, slot_start, slot_end) combinations for idempotency
    existing_result = await db.execute(
        select(RoomSlotInventory.date, RoomSlotInventory.slot_start, RoomSlotInventory.slot_end).where(
            and_(
                RoomSlotInventory.room_variant_id == body.room_variant_id,
                RoomSlotInventory.date >= body.from_date,
                RoomSlotInventory.date <= body.to_date,
            )
        )
    )
    existing_slots = {(r[0], r[1], r[2]) for r in existing_result.all()}

    created = 0
    skipped = 0
    current = body.from_date

    while current <= body.to_date:
        for s_start, s_end in slots_to_generate:
            if (current, s_start, s_end) in existing_slots:
                skipped += 1
            else:
                total_rooms = slot_capacity_map.get((s_start, s_end), default_total_rooms)
                db.add(RoomSlotInventory(
                    room_variant_id=body.room_variant_id,
                    date=current,
                    slot_start=s_start,
                    slot_end=s_end,
                    total_rooms=total_rooms,
                    booked_rooms=0,
                    reserved_rooms=0,
                    is_closed=False,
                ))
                created += 1
        current += timedelta(days=1)

    await db.commit()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="GENERATE_ROOM_INVENTORY",
        entity_type="RoomVariant",
        entity_id=str(body.room_variant_id),
        details={
            "from_date": str(body.from_date),
            "to_date": str(body.to_date),
            "default_total_rooms": default_total_rooms,
            "slot_capacities": [
                {
                    "slot_start": str(slot_start),
                    "slot_end": str(slot_end),
                    "total_rooms": slot_capacity_map.get((slot_start, slot_end), default_total_rooms),
                }
                for slot_start, slot_end in slots_to_generate
            ],
            "created": created,
            "skipped": skipped,
        },
    )
    await db.commit()
    
    # Invalidate cache
    clear_cache_prefix(f"inventory:rooms:{body.room_variant_id}")
    clear_cache_prefix("rooms:")
    room_result = await db.execute(
        select(Room.slug, Room.id).join(RoomVariant, RoomVariant.room_id == Room.id).where(
            RoomVariant.id == body.room_variant_id
        )
    )
    slug_row = room_result.first()
    if slug_row:
        slug = slug_row[0]
        room_id = slug_row[1]
        clear_cache_prefix(f"rooms:detail:{slug}")
        import asyncio
        from app.services.redis_client import invalidate_cached_availability
        asyncio.create_task(invalidate_cached_availability(slug))
        from app.utils.cache import trigger_frontend_revalidation
        trigger_frontend_revalidation(tags=[f"room-{slug}"])

        # Broadcast SSE for newly created room inventory slots
        if created > 0:
            import time
            from app.core.timezone import get_ist_now
            from app.utils.sse import sse_manager
            
            current_date = body.from_date
            while current_date <= body.to_date:
                for s_start, s_end in slots_to_generate:
                    if (current_date, s_start, s_end) not in existing_slots:
                        total_rooms = slot_capacity_map.get((s_start, s_end), default_total_rooms)
                        sse_payload = {
                            "version": int(time.time() * 1000),
                            "timestamp": get_ist_now().isoformat(),
                            "room_id": room_id,
                            "travel_date": str(current_date),
                            "available": total_rooms,
                            "reserved": 0,
                            "booked": 0,
                            "is_closed": False,
                            "variant_id": body.room_variant_id,
                            "slot_start": str(s_start),
                            "slot_end": str(s_end)
                        }
                        await sse_manager.broadcast_event("room", str(room_id), "INVENTORY_UPDATE", sse_payload)
                current_date += timedelta(days=1)

    return RoomInventoryGenerateResponse(
        created=created,
        skipped=skipped,
        message=(
            f"Generated {created} inventory rows for variant '{variant.variant_name}' "
            f"across {len(slots_to_generate)} slot(s), skipped {skipped} existing."
        ),
    )


# ─── Room variant calendar view ───────────────────────────────────────────────

@router.get(
    "/rooms/{room_variant_id}/calendar",
    response_model=List[RoomInventoryRow],
)
async def get_room_calendar(
    room_variant_id: int,
    month: str = Query(..., description="YYYY-MM format"),
    db: AsyncSession = Depends(get_db),
):
    """Get all RoomSlotInventory rows for a specific room variant within a month."""
    try:
        year, mon = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format.")

    from_date = date(year, mon, 1)
    to_date = (
        date(year + 1, 1, 1) - timedelta(days=1) if mon == 12
        else date(year, mon + 1, 1) - timedelta(days=1)
    )

    cache_key = f"inventory:rooms:{room_variant_id}:{month}"

    async def _fetch():
        result = await db.execute(
            select(RoomSlotInventory)
            .where(
                and_(
                    RoomSlotInventory.room_variant_id == room_variant_id,
                    RoomSlotInventory.date >= from_date,
                    RoomSlotInventory.date <= to_date,
                )
            )
            .order_by(RoomSlotInventory.date.asc())
        )
        return [_compute_room_row(r) for r in result.scalars().all()]

    return await ttl_cache_get_or_set(cache_key, 60, _fetch)


# ─── Update a single room variant date ────────────────────────────────────────

@router.patch(
    "/rooms/slots/{slot_id}",
    response_model=RoomInventoryRow,
)
async def update_room_inventory_row(
    slot_id: int,
    body: RoomInventoryUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """
    Update capacity or is_closed for a specific slot row by its ID.
    Hard rule: total_rooms cannot be reduced below booked_rooms.
    """
    result = await db.execute(
        select(RoomSlotInventory).where(RoomSlotInventory.id == slot_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No inventory slot found with ID {slot_id}."
        )

    today = ist_date_today()
    if row.date < today:
        raise HTTPException(
            status_code=400,
            detail="Cannot modify inventory for past dates."
        )

    updates = body.model_dump(exclude_unset=True)

    if "total_rooms" in updates and updates["total_rooms"] < row.booked_rooms:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot reduce capacity to {updates['total_rooms']} — "
                f"{row.booked_rooms} rooms already booked on {row.date} for slot {row.slot_start}-{row.slot_end}."
            ),
        )

    for key, value in updates.items():
        setattr(row, key, value)

    await db.commit()
    await db.refresh(row)

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="UPDATE_ROOM_INVENTORY",
        entity_type="RoomSlotInventory",
        entity_id=str(row.id),
        details={"date": str(row.date), "room_variant_id": row.room_variant_id, "slot_id": slot_id, **updates},
    )
    
    # Broadcast to invalidate caches
    clear_cache_prefix(f"inventory:rooms:{row.room_variant_id}")
    clear_cache_prefix("rooms:")
    from app.services.redis_client import invalidate_cached_availability
    
    # Need to find the slug for this room variant
    room_result = await db.execute(
        select(Room.slug, Room.id).join(RoomVariant, RoomVariant.room_id == Room.id).where(
            RoomVariant.id == row.room_variant_id
        )
    )
    slug_row = room_result.first()
    if slug_row:
        slug = slug_row[0]
        room_id = slug_row[1]
        clear_cache_prefix(f"rooms:detail:{slug}")
        import asyncio
        asyncio.create_task(invalidate_cached_availability(slug))
        from app.utils.cache import trigger_frontend_revalidation
        trigger_frontend_revalidation(tags=[f"room-{slug}"])
        
        # Broadcast SSE for Admin Inventory Edit
        import time
        from app.core.timezone import get_ist_now
        from app.utils.sse import sse_manager
        sse_payload = {
            "version": int(time.time() * 1000),
            "timestamp": get_ist_now().isoformat(),
            "room_id": room_id,
            "travel_date": str(row.date),
            "available": row.total_rooms - (row.booked_rooms + row.reserved_rooms),
            "reserved": row.reserved_rooms,
            "booked": row.booked_rooms,
            "is_closed": row.is_closed,
            "variant_id": row.room_variant_id,
            "slot_start": str(row.slot_start),
            "slot_end": str(row.slot_end)
        }
        await sse_manager.broadcast_event("room", str(room_id), "INVENTORY_UPDATE", sse_payload)
        
    return _compute_room_row(row)


# ─── Delete a single room variant date row ────────────────────────────────────

@router.delete(
    "/rooms/slots/{slot_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_room_inventory_row(
    slot_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Delete a specific inventory row by ID. Blocked if booked_rooms > 0."""
    result = await db.execute(
        select(RoomSlotInventory).where(RoomSlotInventory.id == slot_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Room inventory slot not found.")

    if row.booked_rooms > 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot delete: {row.booked_rooms} rooms already booked for this slot. "
                "Close the slot instead."
            ),
        )

    room_variant_id = row.room_variant_id
    date_str = str(row.date)
    
    # Fetch slug for invalidation
    room_result = await db.execute(
        select(Room.slug, Room.id).join(RoomVariant, RoomVariant.room_id == Room.id).where(
            RoomVariant.id == room_variant_id
        )
    )
    slug_row = room_result.first()

    await db.delete(row)
    await db.commit()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="DELETE_ROOM_INVENTORY",
        entity_type="RoomSlotInventory",
        entity_id=str(slot_id),
        details={"date": date_str, "room_variant_id": room_variant_id},
    )
    
    clear_cache_prefix(f"inventory:rooms:{room_variant_id}")
    clear_cache_prefix("rooms:")
    if slug_row:
        slug = slug_row[0]
        room_id = slug_row[1]
        clear_cache_prefix(f"rooms:detail:{slug}")
        from app.services.redis_client import invalidate_cached_availability
        import asyncio
        asyncio.create_task(invalidate_cached_availability(slug))
        from app.utils.cache import trigger_frontend_revalidation
        trigger_frontend_revalidation(tags=[f"room-{slug}"])
        
        # Broadcast SSE for Admin Inventory Delete
        import time
        from app.core.timezone import get_ist_now
        from app.utils.sse import sse_manager
        sse_payload = {
            "version": int(time.time() * 1000),
            "timestamp": get_ist_now().isoformat(),
            "room_id": room_id,
            "travel_date": date_str,
            "available": 0,
            "reserved": 0,
            "booked": 0,
            "is_closed": True,
            "variant_id": room_variant_id,
            "slot_start": str(row.slot_start),
            "slot_end": str(row.slot_end)
        }
        await sse_manager.broadcast_event("room", str(room_id), "INVENTORY_UPDATE", sse_payload)


    return None


# ─── Transport Inventory Routes ────────────────────────────────────────────────

from app.models.package import PackageTransportOption, PackageTransportInventory
from app.schemas.inventory import (
    TransportInventoryGenerateRequest,
    TransportInventoryGenerateResponse,
    TransportInventoryUpdateRequest,
    TransportInventoryRow,
)


@router.post("/transport/generate", response_model=TransportInventoryGenerateResponse)
async def generate_transport_inventory(
    payload: TransportInventoryGenerateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Bulk-generate transport inventory rows for every transport option on a package
    for a given date range.
    Skips dates that already have a row.
    """
    if payload.from_date > payload.to_date:
        raise HTTPException(status_code=400, detail="from_date must be <= to_date")
    if (payload.to_date - payload.from_date).days > 365:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 365 days")

    # Fetch all transport options for this package
    opts_res = await db.execute(
        select(PackageTransportOption).where(
            PackageTransportOption.package_id == payload.package_id,
            PackageTransportOption.deleted_at.is_(None),
        )
    )
    opts = opts_res.scalars().all()
    if not opts:
        raise HTTPException(status_code=404, detail="No transport options found for this package")

    # Fetch all existing slots in one query to avoid N+1 (including deleted ones)
    existing_res = await db.execute(
        select(PackageTransportInventory).where(
            PackageTransportInventory.transport_option_id.in_([o.id for o in opts]),
            PackageTransportInventory.date >= payload.from_date,
            PackageTransportInventory.date <= payload.to_date,
        )
    )
    existing_rows = { (r.transport_option_id, r.date): r for r in existing_res.scalars().all() }

    created = 0
    skipped = 0
    total_days = (payload.to_date - payload.from_date).days + 1
    created_slots = []

    for opt in opts:
        # Determine count: use caller-supplied count or fallback to option capacity
        opt_count = 1
        if payload.option_counts and str(opt.id) in payload.option_counts:
            opt_count = int(payload.option_counts[str(opt.id)])
        else:
            opt_count = int(opt.capacity or 1)

        for day_offset in range(total_days):
            d = payload.from_date + timedelta(days=day_offset)
            # Check if row already exists
            existing_row = existing_rows.get((opt.id, d))
            if existing_row:
                if existing_row.deleted_at is None:
                    skipped += 1
                    continue
                else:
                    # Restore softly deleted row
                    existing_row.deleted_at = None
                    existing_row.available_count = opt_count
                    existing_row.booked_count = 0
                    existing_row.is_closed = False
                    created += 1
                    created_slots.append((opt.id, d))
            else:
                row = PackageTransportInventory(
                    transport_option_id=opt.id,
                    date=d,
                    available_count=opt_count,
                    booked_count=0,
                    is_closed=False,
                )
                db.add(row)
                created += 1
                created_slots.append((opt.id, d))

    await db.commit()
    
    # Broadcast SSE for generated slots
    from app.utils.sse import broadcast_transport_update
    for opt_id, slot_date in created_slots:
        await broadcast_transport_update(db, opt_id, slot_date)
    
    # Cache invalidation for transport generation
    from app.models.package import Package
    from app.services.redis_client import invalidate_cached_availability
    from app.utils.cache import trigger_frontend_revalidation
    import asyncio
    
    pkg = await db.scalar(select(Package).where(Package.id == payload.package_id))
    if pkg:
        asyncio.create_task(invalidate_cached_availability(pkg.slug))
        trigger_frontend_revalidation(tags=[f"package-{pkg.slug}"])

    return TransportInventoryGenerateResponse(
        created=created,
        skipped=skipped,
        message=f"Generated {created} rows, skipped {skipped} existing rows.",
    )


@router.get("/transport/{package_id}/calendar")
async def get_transport_inventory_calendar(
    package_id: int,
    month: str = Query(..., description="YYYY-MM"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Returns all transport inventory rows for a package's transport options
    in the given month. Groups by date → [option_rows].
    """
    try:
        year, mon = map(int, month.split("-"))
    except Exception:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")

    from_date = date(year, mon, 1)
    import calendar as cal_mod
    last_day = cal_mod.monthrange(year, mon)[1]
    to_date = date(year, mon, last_day)

    # All transport options for this package
    opts_res = await db.execute(
        select(PackageTransportOption).where(
            PackageTransportOption.package_id == package_id,
            PackageTransportOption.deleted_at.is_(None),
        )
    )
    opts = opts_res.scalars().all()
    if not opts:
        return {"options": [], "dates": {}}

    opt_map = {o.id: o for o in opts}
    opt_ids = [o.id for o in opts]

    # Fetch inventory rows
    rows_res = await db.execute(
        select(PackageTransportInventory).where(
            PackageTransportInventory.transport_option_id.in_(opt_ids),
            PackageTransportInventory.date >= from_date,
            PackageTransportInventory.date <= to_date,
            PackageTransportInventory.deleted_at.is_(None),
        ).order_by(PackageTransportInventory.date, PackageTransportInventory.transport_option_id)
    )
    rows = rows_res.scalars().all()

    # Build date-keyed response
    dates: dict = {}
    for row in rows:
        d_str = row.date.isoformat()
        if d_str not in dates:
            dates[d_str] = []
        opt = opt_map.get(row.transport_option_id)
        if opt:
            dates[d_str].append(TransportInventoryRow.from_orm_with_option(row, opt).model_dump())

    options_out = [
        {
            "id": o.id,
            "title": o.title,
            "type": o.type.value if hasattr(o.type, "value") else str(o.type),
            "capacity": o.capacity,
        }
        for o in opts
    ]

    return {"options": options_out, "dates": dates}


@router.patch("/transport/slots/{slot_id}", response_model=TransportInventoryRow)
async def update_transport_inventory_slot(
    slot_id: int,
    payload: TransportInventoryUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Update a single transport inventory slot's count / closed flag / price override."""
    row = await db.scalar(
        select(PackageTransportInventory).where(
            PackageTransportInventory.id == slot_id,
            PackageTransportInventory.deleted_at.is_(None),
        ).with_for_update()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Transport inventory slot not found")

    opt = await db.scalar(
        select(PackageTransportOption).where(PackageTransportOption.id == row.transport_option_id)
    )
    if not opt:
        raise HTTPException(status_code=404, detail="Transport option not found")

    t_type = opt.type.value if hasattr(opt.type, "value") else str(opt.type)
    is_shared = t_type != "SEPARATE_VEHICLE"

    if payload.capacity is not None:
        opt.capacity = payload.capacity

    if payload.available_count is not None or payload.capacity is not None:
        new_avail = payload.available_count if payload.available_count is not None else row.available_count
        new_cap = opt.capacity or 1
        
        total_capacity_new = (new_avail * new_cap) if is_shared else new_avail
        if total_capacity_new < row.booked_count:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot set available_count ({new_avail}) and capacity ({new_cap}) as it results in {total_capacity_new} seats/vehicles, which is below already booked ({row.booked_count})",
            )
        
        if payload.available_count is not None:
            row.available_count = payload.available_count

    if payload.is_closed is not None:
        row.is_closed = payload.is_closed

    if payload.price_override is not None:
        row.price_override = payload.price_override if payload.price_override > 0 else None

    await db.commit()
    await db.refresh(row)
    
    from app.utils.sse import broadcast_transport_update
    await broadcast_transport_update(db, row.transport_option_id, row.date)
    
    from app.models.package import Package
    from app.services.redis_client import invalidate_cached_availability
    from app.utils.cache import trigger_frontend_revalidation
    import asyncio
    if opt:
        pkg = await db.scalar(select(Package).where(Package.id == opt.package_id))
        if pkg:
            asyncio.create_task(invalidate_cached_availability(pkg.slug))
            trigger_frontend_revalidation(tags=[f"package-{pkg.slug}"])
            
    return TransportInventoryRow.from_orm_with_option(row, opt)


@router.post("/transport/slots", response_model=TransportInventoryRow)
async def create_transport_inventory_slot(
    transport_option_id: int,
    slot_date: date,
    available_count: int = 1,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Create a single transport inventory slot for a specific option + date."""
    # Validate option exists
    opt = await db.scalar(
        select(PackageTransportOption).where(
            PackageTransportOption.id == transport_option_id,
            PackageTransportOption.deleted_at.is_(None),
        )
    )
    if not opt:
        raise HTTPException(status_code=404, detail="Transport option not found")

    # Check for duplicate
    existing = await db.scalar(
        select(PackageTransportInventory).where(
            PackageTransportInventory.transport_option_id == transport_option_id,
            PackageTransportInventory.date == slot_date,
            PackageTransportInventory.deleted_at.is_(None),
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="Slot already exists for this option and date")

    row = PackageTransportInventory(
        transport_option_id=transport_option_id,
        date=slot_date,
        available_count=available_count,
        booked_count=0,
        is_closed=False,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    
    from app.utils.sse import broadcast_transport_update
    await broadcast_transport_update(db, row.transport_option_id, row.date)
    
    from app.models.package import Package
    from app.services.redis_client import invalidate_cached_availability
    from app.utils.cache import trigger_frontend_revalidation
    import asyncio
    pkg = await db.scalar(select(Package).where(Package.id == opt.package_id))
    if pkg:
        asyncio.create_task(invalidate_cached_availability(pkg.slug))
        trigger_frontend_revalidation(tags=[f"package-{pkg.slug}"])
        
    return TransportInventoryRow.from_orm_with_option(row, opt)


@router.delete("/transport/slots/{slot_id}", status_code=204)
async def delete_transport_inventory_slot(
    slot_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Delete (soft-delete) a transport inventory slot. This blocks that transport option for the date."""
    from sqlalchemy import func
    row = await db.scalar(
        select(PackageTransportInventory).where(
            PackageTransportInventory.id == slot_id,
            PackageTransportInventory.deleted_at.is_(None),
        ).with_for_update()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Transport inventory slot not found")

    if row.booked_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete slot: {row.booked_count} already booked. Close it instead.",
        )

    option_id = row.transport_option_id
    slot_date = row.date
    row.deleted_at = func.now()
    await db.commit()
    
    from app.utils.sse import broadcast_transport_update
    await broadcast_transport_update(db, option_id, slot_date)
    
    opt = await db.scalar(
        select(PackageTransportOption).where(PackageTransportOption.id == row.transport_option_id)
    )
    if opt:
        from app.models.package import Package
        from app.services.redis_client import invalidate_cached_availability
        from app.utils.cache import trigger_frontend_revalidation
        import asyncio
        pkg = await db.scalar(select(Package).where(Package.id == opt.package_id))
        if pkg:
            asyncio.create_task(invalidate_cached_availability(pkg.slug))
            trigger_frontend_revalidation(tags=[f"package-{pkg.slug}"])
            
    return None

# ─── Bulk Action Endpoints ───────────────────────────────────────────────────

from app.schemas.inventory import (
    InventoryBulkActionRequest,
    RoomInventoryBulkActionRequest,
    TransportInventoryBulkActionRequest,
    BulkActionType,
)

@router.post("/packages/bulk")
async def bulk_action_package_inventory(
    payload: InventoryBulkActionRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    if payload.from_date > payload.to_date:
        raise HTTPException(status_code=400, detail="from_date must be <= to_date")
        
    query = select(PackageVariantInventory).where(
        PackageVariantInventory.variant_id == payload.variant_id,
        PackageVariantInventory.date >= payload.from_date,
        PackageVariantInventory.date <= payload.to_date,
        PackageVariantInventory.deleted_at.is_(None),
    )
    res = await db.execute(query)
    rows = res.scalars().all()
    
    if not rows:
        return {"updated": 0, "message": "No inventory found in the given date range."}
        
    updated = 0
    import datetime
    
    for row in rows:
        if payload.action == BulkActionType.UPDATE_CAPACITY and payload.total_capacity is not None:
            if row.total_capacity == payload.total_capacity:
                continue
            row.total_capacity = payload.total_capacity
            updated += 1
        elif payload.action == BulkActionType.OPEN:
            if not row.is_closed:
                continue
            row.is_closed = False
            updated += 1
        elif payload.action == BulkActionType.CLOSE:
            if row.is_closed:
                continue
            row.is_closed = True
            updated += 1
        elif payload.action == BulkActionType.DELETE:
            if row.deleted_at is not None:
                continue
            row.deleted_at = datetime.datetime.now(datetime.timezone.utc)
            updated += 1
            
    if updated == 0:
        raise HTTPException(
            status_code=400, 
            detail="No slots were modified. They are already in the requested state."
        )
            
    await db.commit()
    await _clear_package_cache_for_variant(db, payload.variant_id)
        
    action_name = payload.action.value if hasattr(payload.action, 'value') else payload.action
    return {"updated": updated, "message": f"Successfully applied {action_name} to {updated} slots."}


@router.post("/rooms/bulk")
async def bulk_action_room_inventory(
    payload: RoomInventoryBulkActionRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    if payload.from_date > payload.to_date:
        raise HTTPException(status_code=400, detail="from_date must be <= to_date")
        
    query = select(RoomSlotInventory).where(
        RoomSlotInventory.room_variant_id == payload.room_variant_id,
        RoomSlotInventory.date >= payload.from_date,
        RoomSlotInventory.date <= payload.to_date,
        RoomSlotInventory.deleted_at.is_(None),
    )
    res = await db.execute(query)
    rows = res.scalars().all()
    
    if not rows:
        return {"updated": 0, "message": "No inventory found in the given date range."}
        
    updated = 0
    import datetime
    
    for row in rows:
        if payload.action == BulkActionType.UPDATE_CAPACITY and payload.total_rooms is not None:
            if row.total_rooms == payload.total_rooms:
                continue
            row.total_rooms = payload.total_rooms
            updated += 1
        elif payload.action == BulkActionType.OPEN:
            if not row.is_closed:
                continue
            row.is_closed = False
            updated += 1
        elif payload.action == BulkActionType.CLOSE:
            if row.is_closed:
                continue
            row.is_closed = True
            updated += 1
        elif payload.action == BulkActionType.DELETE:
            if row.deleted_at is not None:
                continue
            row.deleted_at = datetime.datetime.now(datetime.timezone.utc)
            updated += 1
            
    if updated == 0:
        raise HTTPException(
            status_code=400, 
            detail="No slots were modified. They are already in the requested state."
        )
            
    await db.commit()
    
    # Clear Redis caches
    from app.utils.cache import clear_cache_prefix
    clear_cache_prefix(f"inventory:rooms:{payload.room_variant_id}")
    clear_cache_prefix("rooms:")
    
    from app.models.room import RoomVariant, Room
    from app.services.redis_client import invalidate_cached_availability
    from app.utils.cache import trigger_frontend_revalidation
    import asyncio
    
    room_result = await db.execute(
        select(Room.slug, Room.id).join(RoomVariant, RoomVariant.room_id == Room.id).where(
            RoomVariant.id == payload.room_variant_id
        )
    )
    room_info = room_result.first()
    if room_info:
        slug, room_id = room_info
        clear_cache_prefix(f"rooms:detail:{slug}")
        asyncio.create_task(invalidate_cached_availability(slug))
        trigger_frontend_revalidation(tags=[f"room-{slug}"])

    action_name = payload.action.value if hasattr(payload.action, 'value') else payload.action
    return {"updated": updated, "message": f"Successfully applied {action_name} to {updated} slots."}


@router.post("/transport/bulk")
async def bulk_action_transport_inventory(
    payload: TransportInventoryBulkActionRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    if payload.from_date > payload.to_date:
        raise HTTPException(status_code=400, detail="from_date must be <= to_date")
        
    # Get all transport options for package
    opts_res = await db.execute(
        select(PackageTransportOption).where(
            PackageTransportOption.package_id == payload.package_id,
            PackageTransportOption.deleted_at.is_(None)
        )
    )
    opts = opts_res.scalars().all()
    if not opts:
        return {"updated": 0, "message": "No transport options found for package."}
        
    opt_ids = [o.id for o in opts]
    
    query = select(PackageTransportInventory).where(
        PackageTransportInventory.transport_option_id.in_(opt_ids),
        PackageTransportInventory.date >= payload.from_date,
        PackageTransportInventory.date <= payload.to_date,
        PackageTransportInventory.deleted_at.is_(None),
    )
    res = await db.execute(query)
    rows = res.scalars().all()
    
    if not rows:
        return {"updated": 0, "message": "No transport inventory found in the given date range."}
        
    updated = 0
    import datetime
    
    for row in rows:
        if payload.action == BulkActionType.UPDATE_CAPACITY and payload.option_counts:
            # Only update if a count was provided for this option
            if str(row.transport_option_id) in payload.option_counts:
                new_capacity = int(payload.option_counts[str(row.transport_option_id)])
                if row.available_count == new_capacity:
                    continue
                row.available_count = new_capacity
                updated += 1
        elif payload.action == BulkActionType.OPEN:
            if not row.is_closed:
                continue
            row.is_closed = False
            updated += 1
        elif payload.action == BulkActionType.CLOSE:
            if row.is_closed:
                continue
            row.is_closed = True
            updated += 1
        elif payload.action == BulkActionType.DELETE:
            if row.deleted_at is not None:
                continue
            row.deleted_at = datetime.datetime.now(datetime.timezone.utc)
            updated += 1
            
    if updated == 0:
        raise HTTPException(
            status_code=400, 
            detail="No slots were modified. They are already in the requested state."
        )
            
    await db.commit()
    
    import asyncio
    from app.services.redis_client import invalidate_cached_availability
    from app.utils.cache import trigger_frontend_revalidation
    
    pkg = await db.scalar(select(Package).where(Package.id == payload.package_id))
    if pkg:
        asyncio.create_task(invalidate_cached_availability(pkg.slug))
        trigger_frontend_revalidation(tags=[f"package-{pkg.slug}"])
        
    action_name = payload.action.value if hasattr(payload.action, 'value') else payload.action
    return {"updated": updated, "message": f"Successfully applied {action_name} to {updated} slots."}
