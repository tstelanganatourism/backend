from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, text, delete, update
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

from sqlalchemy.orm import selectinload, with_loader_criteria
from app.models.enums import AdvancePaymentType, PublishStatus
import time as _time_mod

router = APIRouter(
    prefix="/rooms",
    tags=["Admin - Room CMS"],
    dependencies=[Depends(require_admin)]
)


async def purge_room_caches(
    db: Optional[AsyncSession] = None,
    room_id: Optional[int] = None,
    slugs: Optional[List[str]] = None,
    category_slugs: Optional[List[str]] = None,
) -> None:
    """
    Purge every cache layer for accommodations & room categories:
    1. In-process LRU/TTL memory_cache (`rooms_list`, `room_detail`, `room_cats`, `room_cat_detail`, `carousel`)
    2. Redis + utility `_mem_cache` (`rooms:`, `carousel:`, `room_cat_`)
    3. Next.js On-Demand ISR Revalidation webhook (`/api/revalidate`)
    """
    try:
        from app.core.memory_cache import invalidate_mem_cached
        for ns in ("rooms_list", "room_detail", "room_cats", "room_cat_detail", "carousel", "public_stats"):
            invalidate_mem_cached(ns)
    except Exception:
        pass

    try:
        await clear_cache_prefix_async("rooms:")
        await clear_cache_prefix_async("carousel:")
        await clear_cache_prefix_async("room_cat_")
    except Exception:
        pass

    tags = ["rooms", "stays", "categories", "room-categories", "carousel"]
    paths = ["/", "/stays", "/rooms"]

    if room_id:
        tags.append(f"room-{room_id}")
    if slugs:
        for s in slugs:
            if s:
                tags.extend([f"room:{s}", f"stay:{s}", f"room-{s}"])
                paths.extend([f"/stays/{s}", f"/rooms/{s}"])

    resolved_cat_slugs = list(category_slugs or [])
    if db is not None and not resolved_cat_slugs:
        try:
            cat_rows = await db.execute(
                select(RoomCategory.slug).where(RoomCategory.deleted_at.is_(None))
            )
            resolved_cat_slugs = [row[0] for row in cat_rows.all() if row[0]]
        except Exception:
            pass

    for cs in resolved_cat_slugs:
        if cs:
            tags.extend([f"room-category:{cs}", "categories", "room-categories"])
            paths.append(f"/stays/categories/{cs}")

    try:
        from app.utils.cache import trigger_frontend_revalidation
        trigger_frontend_revalidation(tags=list(set(tags)), paths=list(set(paths)))
    except Exception:
        pass

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

async def sync_room_variants(db: AsyncSession, room: Room, input_variants: Optional[List[any]]) -> None:
    """
    Sync RoomVariant rows cleanly without breaking FK references on existing bookings.
    If a previously existing variant has bookings, soft-delete it instead of orphan-deleting.
    """
    if input_variants is None:
        return

    from app.models.booking import Booking
    from datetime import datetime, timezone

    current_list = list(getattr(room, "variants", []) or [])
    current_map = {item.id: item for item in current_list if item.id is not None}
    kept_ids = set()
    new_list = []

    for input_data in input_variants:
        data = input_data if isinstance(input_data, dict) else input_data.model_dump()
        item_id = data.get("id")
        if item_id and item_id in current_map:
            item = current_map[item_id]
            for key, val in data.items():
                if key != "id":
                    setattr(item, key, val)
            item.deleted_at = None
            kept_ids.add(item_id)
            new_list.append(item)
        else:
            data.pop("id", None)
            new_list.append(RoomVariant(**data))

    removed_ids = [vid for vid in current_map if vid not in kept_ids]
    if removed_ids:
        booked_res = await db.execute(
            select(Booking.room_variant_id)
            .where(Booking.room_variant_id.in_(removed_ids))
            .distinct()
        )
        booked_ids = {row[0] for row in booked_res.all()}
        now_ts = datetime.now(timezone.utc)
        for vid in removed_ids:
            if vid in booked_ids:
                item = current_map[vid]
                item.is_active = False
                item.deleted_at = now_ts
                new_list.append(item)

    setattr(room, "variants", new_list)


class RoomUpdate(BaseModel):
    lodge_name: Optional[str] = None
    public_title: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    address: Optional[str] = None
    map_url: Optional[str] = None
    facilities: Optional[List[str]] = None
    cover_image_url: Optional[str] = None
    video_url: Optional[str] = None
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
    category_ids: Optional[List[int]] = None

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
    is_default = (not search and not status_filter)
    if is_default:
        from app.utils.cache import _mem_get
        cached = _mem_get(f"rooms:admin_list:{limit}:{offset}")
        if cached is not None:
            return cached

    base_query = select(Room).where(Room.deleted_at.is_(None))
    
    if search:
        fts_vector = func.to_tsvector(
            text("'english'::regconfig"),
            func.coalesce(Room.public_title, '') + ' ' + Room.lodge_name + ' ' + func.coalesce(Room.address, '') + ' ' + func.coalesce(Room.description, '')
        )
        base_query = base_query.where(
            or_(
                fts_vector.op('@@')(func.websearch_to_tsquery(text("'english'::regconfig"), search)),
                func.coalesce(Room.public_title, '').ilike(f"%{search}%"),
                Room.lodge_name.ilike(f"%{search}%"),
            )
        )
        
    if status_filter:
        base_query = base_query.where(Room.status == status_filter)
        
    count_query = base_query.with_only_columns(func.count()).order_by(None)
    total_result = await db.execute(count_query)
    total_count = total_result.scalar_one()

    query = base_query.options(
        selectinload(Room.variants),
        selectinload(Room.categories),
        with_loader_criteria(RoomCategory, RoomCategory.deleted_at.is_(None), include_aliases=True),
        with_loader_criteria(RoomVariant, RoomVariant.deleted_at.is_(None), include_aliases=True),
    )
    query = query.order_by(Room.order_priority.asc(), Room.id.asc()).limit(limit).offset(offset)
    
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
    
    res_data = {
        "items": items,
        "total": total_count,
        "page": (offset // limit) + 1 if limit > 0 else 1,
        "size": limit
    }
    if is_default:
        from app.utils.cache import _mem_set
        _mem_set(f"rooms:admin_list:{limit}:{offset}", res_data, ttl_seconds=20)
    return res_data

def full_room_options():
    return (
        selectinload(Room.variants),
        selectinload(Room.categories),
        selectinload(Room.gallery),
        selectinload(Room.highlights),
        selectinload(Room.faqs),
        selectinload(Room.policies),
        with_loader_criteria(RoomCategory, RoomCategory.deleted_at.is_(None), include_aliases=True),
        with_loader_criteria(RoomVariant, RoomVariant.deleted_at.is_(None), include_aliases=True),
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
                public_title=r.public_title,
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
        slug = f"{slug}-{int(_time_mod.time())}"
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
    await purge_room_caches(db, category_slugs=[cat.slug])
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
    result = await db.execute(
        select(RoomCategory)
        .where(RoomCategory.id == category_id, RoomCategory.deleted_at.is_(None))
        .options(selectinload(RoomCategory.rooms))
    )
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Room category not found")
    old_slug = cat.slug
    for key, val in body.model_dump(exclude_none=True).items():
        setattr(cat, key, val)
    await db.commit()
    await db.refresh(cat)
    await purge_room_caches(db, category_slugs=[old_slug, cat.slug])
    active_count = len([r for r in cat.rooms if not r.deleted_at])
    return RoomCategoryResponse(
        id=cat.id, name=cat.name, slug=cat.slug,
        description=cat.description, cover_image_url=cat.cover_image_url,
        icon=cat.icon, sort_order=cat.sort_order, is_active=cat.is_active,
        room_count=active_count,
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
    cat_slug = cat.slug
    cat.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    await purge_room_caches(db, category_slugs=[cat_slug])

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
    await purge_room_caches(db, slugs=[r.slug for r in rooms], category_slugs=[cat.slug])
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
    removed_slugs = [r.slug for r in cat.rooms if r.id == room_id and r.slug]
    cat.rooms = [r for r in cat.rooms if r.id != room_id]
    await db.commit()
    await purge_room_caches(db, slugs=removed_slugs, category_slugs=[cat.slug])
    return {"removed": room_id}

router.include_router(room_category_router)


class RoomReorderItem(BaseModel):
    id: int
    order_priority: int


@router.put("/reorder", status_code=200)
async def reorder_rooms(
    items: List[RoomReorderItem],
    db: AsyncSession = Depends(get_db)
):
    """
    Bulk update order_priority for rooms/lodges.
    Instantly clears public discovery memory caches.
    """
    if not items:
        return {"status": "success", "updated": 0}
        
    for item in items:
        await db.execute(
            update(Room)
            .where(Room.id == item.id)
            .values(order_priority=item.order_priority)
        )
    await db.commit()
    
    await purge_room_caches(db)
    
    return {"status": "success", "updated": len(items)}


@router.post("/cache/clear", status_code=200)
async def clear_rooms_cache(
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Admin endpoint to flush all room and category caches and revalidate storefront."""
    await purge_room_caches(db)
    return {"status": "success", "message": "All room and category caches purged and storefront revalidation triggered."}


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
    title_for_slug = body.public_title or body.lodge_name
    slug = slugify(body.slug) if body.slug else slugify(title_for_slug)
    
    existing = await db.execute(select(Room).where(Room.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{int(_time_mod.time())}"
        
    room_data = body.model_dump(exclude={
        "variants", "gallery", "highlights", "faqs", "policies", "category_ids"
    })
    room_data["slug"] = slug
    if room_data.get("public_title"):
        room_data["public_title"] = str(room_data["public_title"]).strip() or None
    elif room_data.get("lodge_name"):
        from app.utils.room_display import looks_like_vendor_name
        if not looks_like_vendor_name(room_data["lodge_name"]):
            room_data["public_title"] = str(room_data["lodge_name"]).strip()
    
    # Auto-seed default office address & map if not specified
    if not room_data.get("address"):
        room_data["address"] = "Door No. 10-1-2/1, Ground Floor, Om Shanthi Building Sataram, Kalyana Mandapam Road, near SBI ATM, Bhadrachalam, Telangana 507111"
    if not room_data.get("map_url"):
        room_data["map_url"] = '<iframe src="https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d3801.6249326203515!2d80.8842764!3d17.6679145!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x3a36a9430aa06acf%3A0xc16caef81e0d09c9!2sTelangana%20Boat%20Tourism!5e0!3m2!1sen!2sin!4v1782409791475!5m2!1sen!2sin" width="600" height="450" style="border:0;" allowfullscreen="" loading="lazy" referrerpolicy="strict-origin-when-cross-origin"></iframe>'

    room = Room(**room_data)
    
    # Sync child relations (filtering out empty/blank entries)
    valid_gallery = [g for g in (body.gallery or []) if getattr(g, 'image_url', None) and str(g.image_url).strip()]
    valid_highlights = [h for h in (body.highlights or []) if getattr(h, 'title', None) and str(h.title).strip()]
    valid_faqs = [f for f in (body.faqs or []) if getattr(f, 'question', None) and str(f.question).strip() and getattr(f, 'answer', None) and str(f.answer).strip()]
    valid_policies = [p for p in (body.policies or []) if getattr(p, 'title', None) and str(p.title).strip() and getattr(p, 'description', None) and str(p.description).strip()]

    await sync_nested_relation(db, room, "variants", RoomVariant, body.variants)
    await sync_nested_relation(db, room, "gallery", RoomGalleryImage, valid_gallery)
    await sync_nested_relation(db, room, "highlights", RoomHighlight, valid_highlights)
    await sync_nested_relation(db, room, "faqs", RoomFAQ, valid_faqs)
    await sync_nested_relation(db, room, "policies", RoomPolicy, valid_policies)

    if body.category_ids:
        cat_res = await db.execute(
            select(RoomCategory).where(RoomCategory.id.in_(body.category_ids), RoomCategory.deleted_at.is_(None))
        )
        room.categories = list(cat_res.scalars().all())

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
        details={"lodge_name": room.lodge_name, "public_title": room.public_title, "slug": room.slug}
    )
    await db.commit()
    await purge_room_caches(db, room_id=room.id, slugs=[room.slug])
    
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
        "variants", "gallery", "highlights", "faqs", "policies", "category_ids"
    })
    
    if "public_title" in update_data and update_data["public_title"] is not None:
        update_data["public_title"] = str(update_data["public_title"]).strip() or None

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
        
    # Sync child relations if provided (filtering out empty/blank entries)
    if body.variants is not None:
        await sync_room_variants(db, room, body.variants)
    if body.gallery is not None:
        valid_gallery = [g for g in body.gallery if getattr(g, 'image_url', None) and str(g.image_url).strip()]
        await sync_nested_relation(db, room, "gallery", RoomGalleryImage, valid_gallery)
    if body.highlights is not None:
        valid_highlights = [h for h in body.highlights if getattr(h, 'title', None) and str(h.title).strip()]
        await sync_nested_relation(db, room, "highlights", RoomHighlight, valid_highlights)
    if body.faqs is not None:
        valid_faqs = [f for f in body.faqs if getattr(f, 'question', None) and str(f.question).strip() and getattr(f, 'answer', None) and str(f.answer).strip()]
        await sync_nested_relation(db, room, "faqs", RoomFAQ, valid_faqs)
    if body.policies is not None:
        valid_policies = [p for p in body.policies if getattr(p, 'title', None) and str(p.title).strip() and getattr(p, 'description', None) and str(p.description).strip()]
        await sync_nested_relation(db, room, "policies", RoomPolicy, valid_policies)
    if body.category_ids is not None:
        cat_res = await db.execute(
            select(RoomCategory).where(RoomCategory.id.in_(body.category_ids), RoomCategory.deleted_at.is_(None))
        )
        room.categories = list(cat_res.scalars().all())

    # Recompute starting_price from active non-deleted variants
    room.starting_price = min(
        (v.weekday_price for v in room.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'weekday_price', 0) > 0),
        default=0
    )
    room.starting_weekend_price = min(
        (v.weekend_price for v in room.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'weekend_price', 0) > 0),
        default=None
    )

    await db.commit()
    
    audit_details = body.model_dump(mode="json", exclude_unset=True, exclude={
        "variants", "gallery", "highlights", "faqs", "policies"
    })
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="UPDATE_ROOM",
        entity_type="Room",
        entity_id=str(room.id),
        details=audit_details
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
        
    await purge_room_caches(db, room_id=room.id, slugs=[old_slug, room.slug])
    
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
        details={"lodge_name": room.lodge_name, "public_title": room.public_title}
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
    
    await purge_room_caches(db, room_id=room.id, slugs=[room.slug])
    
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

