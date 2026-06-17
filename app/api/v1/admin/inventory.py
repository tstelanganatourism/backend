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
