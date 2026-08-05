from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, text, delete
from typing import List, Optional
from datetime import time

from app.db.session import get_db
from app.models.room import (
    Room, RoomVariant, RoomGalleryImage, RoomHighlight, RoomFAQ, RoomPolicy,
    RoomCategory, room_category_assignments,
)
from app.schemas.room import (
    RoomBase, RoomResponse, RoomDetailResponse, RoomCreate, RoomBookingSlotSchema,
    RoomVariantInput, RoomGalleryImageInput, RoomHighlightInput, RoomFAQInput, RoomPolicyInput, RoomPaginatedResponse,
    RoomCategoryCreate, RoomCategoryUpdate, RoomCategoryResponse, RoomCategoryDetailResponse, RoomCategoryAssignRequest,
)
from app.middleware.auth import require_admin
from app.models.user import User
from app.utils.audit import log_action
from app.utils.cache import clear_cache_prefix, clear_cache_prefix_async
from pydantic import BaseModel, ConfigDict
import re

from sqlalchemy.orm import selectinload
from app.models.enums import AdvancePaymentType

router = APIRouter(
    prefix="/rooms",
    tags=["Admin - Room CMS"],
    dependencies=[Depends(require_admin)]
)

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text

async def sync_nested_relation(db: AsyncSession, room: Room, relation_name: str, model_class: any, input_data_list: Optional[List[any]]):
    """
    Syncs a nested one-to-many relationship using in-place list modification.
    """
    current_list = getattr(room, relation_name)
    current_map = {item.id: item for item in current_list if item.id is not None}
    
    new_list = []
    
    for input_data in (input_data_list or []):
        data = input_data if isinstance(input_data, dict) else input_data.model_dump()
        item_id = data.get("id")
        
        if item_id and item_id in current_map:
            # Update existing
            item = current_map[item_id]
            for key, val in data.items():
                if key != "id":
                    setattr(item, key, val)
            new_list.append(item)
        else:
            # Create new
            data.pop("id", None)
            new_item = model_class(**data)
            new_list.append(new_item)
            
    setattr(room, relation_name, new_list)

class RoomUpdate(BaseModel):
    lodge_name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    address: Optional[str] = None
    map_url: Optional[str] = None
    facilities: Optional[List[str]] = None
    cover_image_url: Optional[str] = None
    total_rooms: Optional[int] = None
    slot_start: Optional[time] = None
    slot_end: Optional[time] = None
    order_priority: Optional[int] = None
    is_featured: Optional[bool] = None
    is_active: Optional[bool] = None
    status: Optional[str] = None
    
    advance_payment_type: Optional[AdvancePaymentType] = None
    advance_payment_value: Optional[float] = None
    
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    og_image_url: Optional[str] = None
    canonical_url: Optional[str] = None
    booking_slots: Optional[List[RoomBookingSlotSchema]] = None

    variants: Optional[List[RoomVariantInput]] = None
    gallery: Optional[List[RoomGalleryImageInput]] = None
    highlights: Optional[List[RoomHighlightInput]] = None
    faqs: Optional[List[RoomFAQInput]] = None
    policies: Optional[List[RoomPolicyInput]] = None
    
    model_config = ConfigDict(from_attributes=True)

@router.get("", response_model=RoomPaginatedResponse)
async def list_rooms(
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db)
):
    """List all lodges/rooms with optional search and status filtering."""
    base_query = select(Room).where(Room.deleted_at.is_(None))
    
    if search:
        fts_vector = func.to_tsvector(text("'english'::regconfig"), Room.lodge_name + ' ' + func.coalesce(Room.address, '') + ' ' + func.coalesce(Room.description, ''))
        base_query = base_query.where(
            fts_vector.op('@@')(func.websearch_to_tsquery(text("'english'::regconfig"), search))
        )
        
    if status_filter:
        base_query = base_query.where(Room.status == status_filter)
        
    count_query = base_query.with_only_columns(func.count()).order_by(None)
    total_result = await db.execute(count_query)
    total_count = total_result.scalar_one()

    query = base_query.options(
        selectinload(Room.variants),
        selectinload(Room.categories)
    )
    query = query.order_by(Room.order_priority.desc(), Room.created_at.desc()).limit(limit).offset(offset)
    
    result = await db.execute(query)
    items = result.scalars().all()
    
    room_ids = [item.id for item in items]
    if room_ids:
        from app.models.booking import Booking
        from app.models.enums import BookingStatus
        booking_counts = await db.execute(
            select(RoomVariant.room_id, func.count(Booking.id))
            .join(Booking, Booking.room_variant_id == RoomVariant.id)
            .where(RoomVariant.room_id.in_(room_ids))
            .where(Booking.status.not_in([BookingStatus.CANCELLED, BookingStatus.REFUNDED]))
            .group_by(RoomVariant.room_id)
        )
        counts_map = dict(booking_counts.all())
        for item in items:
            item.active_booking_count = counts_map.get(item.id, 0)
    else:
        for item in items:
            item.active_booking_count = 0
    
    return {
        "items": items,
        "total": total_count,
        "page": (offset // limit) + 1 if limit > 0 else 1,
        "size": limit
    }

def full_room_options():
    return (
        selectinload(Room.variants),
        selectinload(Room.categories),
        selectinload(Room.gallery),
        selectinload(Room.highlights),
        selectinload(Room.faqs),
        selectinload(Room.policies)
    )


# ── Admin Room Categories Sub-Router (MUST be before /{room_id}) ──────────────

room_category_router = APIRouter(
    prefix="/categories",
    tags=["Admin - Room Categories"],
    dependencies=[Depends(require_admin)]
)

@room_category_router.get("", response_model=List[RoomCategoryDetailResponse])
async def list_room_categories(db: AsyncSession = Depends(get_db)):
    """List all room categories with their rooms."""
    result = await db.execute(
        select(RoomCategory)
        .where(RoomCategory.deleted_at.is_(None))
        .options(selectinload(RoomCategory.rooms).selectinload(Room.variants))
        .order_by(RoomCategory.sort_order, RoomCategory.id)
    )
    categories = result.scalars().all()
    return [
        RoomCategoryDetailResponse(
            id=cat.id,
            name=cat.name,
            slug=cat.slug,
            description=cat.description,
            cover_image_url=cat.cover_image_url,
            icon=cat.icon,
            sort_order=cat.sort_order,
            is_active=cat.is_active,
            room_count=len([r for r in cat.rooms if not r.deleted_at]),
            rooms=[RoomResponse(
                id=r.id,
                lodge_name=r.lodge_name,
                slug=r.slug,
                description=r.description,
                address=r.address,
                facilities=r.facilities or [],
                starting_price=r.starting_price,
                starting_weekend_price=r.starting_weekend_price,
                total_rooms=r.total_rooms,
                slot_start=r.slot_start,
                slot_end=r.slot_end,
                is_featured=r.is_featured,
                is_active=r.is_active,
                status=r.status,
                order_priority=r.order_priority,
                cover_image_url=r.cover_image_url,
                advance_payment_type=r.advance_payment_type,
                advance_payment_value=r.advance_payment_value,
                created_at=r.created_at,
                updated_at=r.updated_at,
                variants=[],
            ) for r in cat.rooms if not r.deleted_at],
        )
        for cat in categories
    ]

@room_category_router.post("", response_model=RoomCategoryResponse, status_code=201)
async def create_room_category(
    body: RoomCategoryCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new room category."""
    slug = body.slug or slugify(body.name)
    existing = await db.execute(select(RoomCategory).where(RoomCategory.slug == slug, RoomCategory.deleted_at.is_(None)))
    if existing.scalar_one_or_none():
        import time as _time
        slug = f"{slug}-{int(_time.time())}"
    cat = RoomCategory(
        name=body.name,
        slug=slug,
        description=body.description,
        cover_image_url=body.cover_image_url,
        icon=body.icon,
        sort_order=body.sort_order,
        is_active=body.is_active,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    await clear_cache_prefix_async("rooms:")
    return RoomCategoryResponse(
        id=cat.id, name=cat.name, slug=cat.slug,
        description=cat.description, cover_image_url=cat.cover_image_url,
        icon=cat.icon, sort_order=cat.sort_order, is_active=cat.is_active,
        room_count=0,
    )

@room_category_router.patch("/{category_id}", response_model=RoomCategoryResponse)
async def update_room_category(
    category_id: int,
    body: RoomCategoryUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update a room category."""
    result = await db.execute(select(RoomCategory).where(RoomCategory.id == category_id, RoomCategory.deleted_at.is_(None)))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Room category not found")
    for key, val in body.model_dump(exclude_none=True).items():
        setattr(cat, key, val)
    await db.commit()
    await db.refresh(cat)
    await clear_cache_prefix_async("rooms:")
    return RoomCategoryResponse(
        id=cat.id, name=cat.name, slug=cat.slug,
        description=cat.description, cover_image_url=cat.cover_image_url,
        icon=cat.icon, sort_order=cat.sort_order, is_active=cat.is_active,
        room_count=0,
    )

@room_category_router.delete("/{category_id}", status_code=204)
async def delete_room_category(
    category_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Soft-delete a room category."""
    result = await db.execute(select(RoomCategory).where(RoomCategory.id == category_id, RoomCategory.deleted_at.is_(None)))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Room category not found")
    from datetime import datetime, timezone
    cat.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    await clear_cache_prefix_async("rooms:")

@room_category_router.post("/{category_id}/rooms", status_code=200)
async def assign_rooms_to_category(
    category_id: int,
    body: RoomCategoryAssignRequest,
    db: AsyncSession = Depends(get_db)
):
    """Assign (add) rooms to a category."""
    result = await db.execute(select(RoomCategory).where(RoomCategory.id == category_id, RoomCategory.deleted_at.is_(None)).options(selectinload(RoomCategory.rooms)))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Room category not found")
    rooms_result = await db.execute(select(Room).where(Room.id.in_(body.room_ids), Room.deleted_at.is_(None)))
    rooms = rooms_result.scalars().all()
    existing_ids = {r.id for r in cat.rooms}
    for room in rooms:
        if room.id not in existing_ids:
            cat.rooms.append(room)
    await db.commit()
    await clear_cache_prefix_async("rooms:")
    return {"assigned": len(rooms)}

@room_category_router.delete("/{category_id}/rooms/{room_id}", status_code=200)
async def remove_room_from_category(
    category_id: int,
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Remove a specific room from a category."""
    result = await db.execute(select(RoomCategory).where(RoomCategory.id == category_id, RoomCategory.deleted_at.is_(None)).options(selectinload(RoomCategory.rooms)))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Room category not found")
    cat.rooms = [r for r in cat.rooms if r.id != room_id]
    await db.commit()
    await clear_cache_prefix_async("rooms:")
    return {"removed": room_id}

router.include_router(room_category_router)


@router.get("/{room_id}", response_model=RoomDetailResponse)
async def get_room(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get detailed information for a single room/lodge with all relationships loaded."""
    query = (
        select(Room)
        .where(Room.id == room_id, Room.deleted_at.is_(None))
        .options(*full_room_options())
    )
    result = await db.execute(query)
    room = result.scalar_one_or_none()
    
    if not room:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room/Lodge not found"
        )
        
    return room

@router.post("", response_model=RoomDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_room(
    body: RoomCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Create a new room/lodge with all nested relations in one transaction."""
    slug = slugify(body.slug) if body.slug else slugify(body.lodge_name)
    
    existing = await db.execute(select(Room).where(Room.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{int(func.now().select().scalar_one().timestamp())}"
        
    room_data = body.model_dump(exclude={
        "variants", "gallery", "highlights", "faqs", "policies"
    })
    room_data["slug"] = slug
    
    room = Room(**room_data)
    
    # Sync child relations
    await sync_nested_relation(db, room, "variants", RoomVariant, body.variants)
    await sync_nested_relation(db, room, "gallery", RoomGalleryImage, body.gallery)
    await sync_nested_relation(db, room, "highlights", RoomHighlight, body.highlights)
    await sync_nested_relation(db, room, "faqs", RoomFAQ, body.faqs)
    await sync_nested_relation(db, room, "policies", RoomPolicy, body.policies)

    # Compute starting_price
    room.starting_price = min(
        (v.weekday_price for v in room.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'weekday_price', 0) > 0),
        default=0
    )
    room.starting_weekend_price = min(
        (v.weekend_price for v in room.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'weekend_price', 0) > 0),
        default=None
    )

    db.add(room)
    await db.commit()

    # Reload room with all options
    query = select(Room).where(Room.id == room.id).options(*full_room_options())
    result = await db.execute(query)
    room = result.scalar_one()
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="CREATE_ROOM",
        entity_type="Room",
        entity_id=str(room.id),
        details={"lodge_name": room.lodge_name, "slug": room.slug}
    )
    await db.commit()
    clear_cache_prefix("rooms:")
    clear_cache_prefix("carousel:")
    from app.utils.cache import trigger_frontend_revalidation
    trigger_frontend_revalidation(tags=[f"room-{room.id}"])
    
    return room

@router.put("/{room_id}", response_model=RoomDetailResponse)
async def update_room(
    room_id: int,
    body: RoomUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Update an existing room/lodge and all nested relations in one transaction."""
    query = (
        select(Room)
        .where(Room.id == room_id, Room.deleted_at.is_(None))
        .options(*full_room_options())
    )
    result = await db.execute(query)
    room = result.scalar_one_or_none()
    
    if not room:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room/Lodge not found"
        )
        
    update_data = body.model_dump(exclude_unset=True, exclude={
        "variants", "gallery", "highlights", "faqs", "policies"
    })
    
    if "slug" in update_data and update_data["slug"]:
        update_data["slug"] = slugify(update_data["slug"])
        if update_data["slug"] != room.slug:
            existing = await db.execute(select(Room).where(Room.slug == update_data["slug"]))
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Slug already in use"
                )
            
    old_slug = room.slug

    for key, value in update_data.items():
        setattr(room, key, value)
        
    # Sync child relations if provided
    if body.variants is not None:
        await sync_nested_relation(db, room, "variants", RoomVariant, body.variants)
    if body.gallery is not None:
        await sync_nested_relation(db, room, "gallery", RoomGalleryImage, body.gallery)
    if body.highlights is not None:
        await sync_nested_relation(db, room, "highlights", RoomHighlight, body.highlights)
    if body.faqs is not None:
        await sync_nested_relation(db, room, "faqs", RoomFAQ, body.faqs)
    if body.policies is not None:
        await sync_nested_relation(db, room, "policies", RoomPolicy, body.policies)

    # Recompute starting_price
    room.starting_price = min(
        (v.weekday_price for v in room.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'weekday_price', 0) > 0),
        default=0
    )
    room.starting_weekend_price = min(
        (v.weekend_price for v in room.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'weekend_price', 0) > 0),
        default=None
    )

    await db.commit()
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="UPDATE_ROOM",
        entity_type="Room",
        entity_id=str(room.id),
        details=update_data
    )
    await db.commit()
    
    # Broadcast SSE for Admin Room Edit if inactive
    if not room.is_active:
        import time
        from app.core.timezone import get_ist_now
        from app.utils.sse import sse_manager
        sse_payload = {
            "version": int(time.time() * 1000),
            "timestamp": get_ist_now().isoformat(),
            "room_id": room.id,
            "status": "INACTIVE"
        }
        await sse_manager.broadcast_event("room", str(room.id), "ENTITY_STATUS_UPDATE", sse_payload)
        
    clear_cache_prefix("rooms:list:")
    clear_cache_prefix(f"rooms:detail:{old_slug}")
    clear_cache_prefix(f"rooms:detail:{room.slug}")
    clear_cache_prefix("carousel:")
    from app.utils.cache import trigger_frontend_revalidation
    trigger_frontend_revalidation(tags=[f"room-{room.id}"])
    
    # Reload room with all options to prevent expired attributes during Pydantic serialization
    refresh_query = select(Room).where(Room.id == room.id).options(*full_room_options())
    refresh_result = await db.execute(refresh_query)
    room = refresh_result.scalar_one()
    
    return room

@router.delete("/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room(
    room_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Delete a room/lodge with audit logging."""
    result = await db.execute(select(Room).where(Room.id == room_id))
    room = result.scalar_one_or_none()
    
    if not room:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room/Lodge not found"
        )
        
    room.deleted_at = func.now()
    await db.commit()
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="DELETE_ROOM",
        entity_type="Room",
        entity_id=str(room.id),
        details={"lodge_name": room.lodge_name}
    )
    await db.commit()
    
    # Broadcast SSE for Admin Room Delete
    import time
    from app.core.timezone import get_ist_now
    from app.utils.sse import sse_manager
    sse_payload = {
        "version": int(time.time() * 1000),
        "timestamp": get_ist_now().isoformat(),
        "room_id": room_id,
        "status": "DELETED"
    }
    await sse_manager.broadcast_event("room", str(room_id), "ENTITY_STATUS_UPDATE", sse_payload)
    
    clear_cache_prefix("rooms:list:")
    clear_cache_prefix(f"rooms:detail:{room.slug}")
    from app.utils.cache import trigger_frontend_revalidation
    trigger_frontend_revalidation(tags=[f"room-{room.id}"])
    
    return None

@router.get("/{room_id}/future-bookings")
async def get_future_bookings(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Retrieve all future active bookings for this room/lodge."""
    # First, get all variant IDs for this room
    variant_query = select(RoomVariant.id).where(RoomVariant.room_id == room_id)
    variant_result = await db.execute(variant_query)
    variant_ids = variant_result.scalars().all()
    
    if not variant_ids:
        return []
        
    # Query future active bookings
    from datetime import date
    from app.models.booking import Booking
    from app.models.enums import BookingStatus
    
    booking_query = (
        select(Booking)
        .where(
            Booking.room_variant_id.in_(variant_ids),
            Booking.travel_date >= date.today(),
            Booking.status.not_in([BookingStatus.CANCELLED, BookingStatus.REFUNDED])
        )
        .order_by(Booking.travel_date.asc())
    )
    
    booking_result = await db.execute(booking_query)
    bookings = booking_result.scalars().all()
    
    # Return formatted bookings
    return [
        {
            "id": b.id,
            "public_id": b.public_id,
            "travel_date": b.travel_date.isoformat(),
            "adult_count": b.adult_count,
            "child_count": b.child_count,
            "total_amount": float(b.total_amount),
            "status": b.status,
        }
        for b in bookings
    ]


# ── Admin Room Category CRUD ──────────────────────────────────────────────────

