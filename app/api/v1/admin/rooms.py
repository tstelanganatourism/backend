from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, text
from typing import List, Optional
from datetime import time

from app.db.session import get_db
from app.models.room import (
    Room, RoomVariant, RoomGalleryImage, RoomHighlight, RoomFAQ, RoomPolicy
)
from app.schemas.room import (
    RoomBase, RoomResponse, RoomDetailResponse, RoomCreate, RoomBookingSlotSchema,
    RoomVariantInput, RoomGalleryImageInput, RoomHighlightInput, RoomFAQInput, RoomPolicyInput, RoomPaginatedResponse
)
from app.middleware.auth import require_admin
from app.models.user import User
from app.utils.audit import log_action
from app.utils.cache import clear_cache_prefix
from pydantic import BaseModel, ConfigDict
import re

from sqlalchemy.orm import selectinload

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
        
    count_query = select(func.count()).select_from(base_query.subquery())
    total_result = await db.execute(count_query)
    total_count = total_result.scalar_one()

    query = base_query.options(selectinload(Room.variants))
    query = query.order_by(Room.order_priority.desc(), Room.created_at.desc()).limit(limit).offset(offset)
    
    result = await db.execute(query)
    items = result.scalars().all()
    
    return {
        "items": items,
        "total": total_count,
        "page": (offset // limit) + 1 if limit > 0 else 1,
        "size": limit
    }

@router.get("/{room_id}", response_model=RoomDetailResponse)
async def get_room(
    room_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get detailed information for a single room/lodge with all relationships loaded."""
    query = (
        select(Room)
        .where(Room.id == room_id, Room.deleted_at.is_(None))
        .options(
            selectinload(Room.variants),
            selectinload(Room.gallery),
            selectinload(Room.highlights),
            selectinload(Room.faqs),
            selectinload(Room.policies)
        )
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
    slug = body.slug or slugify(body.lodge_name)
    
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

    db.add(room)
    await db.commit()

    # Reload room
    query = (
        select(Room)
        .where(Room.id == room.id)
        .options(
            selectinload(Room.variants),
            selectinload(Room.gallery),
            selectinload(Room.highlights),
            selectinload(Room.faqs),
            selectinload(Room.policies)
        )
    )
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
        .options(
            selectinload(Room.variants),
            selectinload(Room.gallery),
            selectinload(Room.highlights),
            selectinload(Room.faqs),
            selectinload(Room.policies)
        )
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
    
    if "slug" in update_data and update_data["slug"] != room.slug:
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

    await db.commit()
    await db.refresh(room)
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="UPDATE_ROOM",
        entity_type="Room",
        entity_id=str(room.id),
        details=update_data
    )
    await db.commit()
    clear_cache_prefix("rooms:list:")
    clear_cache_prefix(f"rooms:detail:{old_slug}")
    clear_cache_prefix(f"rooms:detail:{room.slug}")
    
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
    clear_cache_prefix("rooms:list:")
    clear_cache_prefix(f"rooms:detail:{room.slug}")
    
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
            Booking.status != BookingStatus.CANCELLED
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
