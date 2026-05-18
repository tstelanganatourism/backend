from typing import Optional, List
from fastapi import APIRouter, Depends, Query, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.room import Room, RoomVariant
from app.schemas.public import PaginatedResponse, RoomListDTO, RoomDetailDTO, RoomVariantPublicDTO
from app.utils.cache import set_public_cache_headers, ttl_cache_get_or_set
from app.models.enums import PublishStatus

router = APIRouter()
PUBLIC_CACHE_TTL_SECONDS = 60

@router.get("", response_model=PaginatedResponse[RoomListDTO])
async def get_rooms(
    response: Response,
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=50, description="Items per page"),
    is_featured: Optional[bool] = Query(None, description="Filter featured only"),
    facilities: Optional[List[str]] = Query(None, description="Filter by facilities"),
    sort: Optional[str] = Query("priority", description="Sort by: priority, price_low, price_high"),
    q: Optional[str] = Query(None, description="Search term for lodge name/address")
):
    """
    Public Room Discovery API.
    Returns a paginated list of active rooms/lodges.
    """
    cache_key = f"rooms:list:{page}:{size}:{is_featured}:{tuple(facilities or [])}:{sort}:{q or ''}"
    set_public_cache_headers(response)

    async def load_rooms() -> PaginatedResponse[RoomListDTO]:
        offset = (page - 1) * size

        # Base Query (Only ACTIVE and PUBLISHED rooms)
        base_query = select(Room).where(
            Room.status == PublishStatus.PUBLISHED,
            Room.is_active == True,
            Room.deleted_at.is_(None)
        )

        # Filters
        if is_featured is not None:
            base_query = base_query.where(Room.is_featured == is_featured)
        if q:
            search_pattern = f"%{q}%"
            base_query = base_query.where(
                or_(
                    Room.lodge_name.ilike(search_pattern),
                    Room.address.ilike(search_pattern),
                    Room.description.ilike(search_pattern)
                )
            )
        
        if facilities:
            # Filter rooms that have ANY of the requested facilities (OR logic)
            base_query = base_query.where(
                or_(*(Room.facilities.contains([f]) for f in facilities))
            )

        # Count Query
        count_query = select(func.count()).select_from(base_query.subquery())
        total_result = await db.execute(count_query)
        total_count = total_result.scalar_one()

        # Subquery to get minimum price per lodge
        price_subquery = (
            select(
                RoomVariant.room_id,
                func.min(RoomVariant.weekday_price).label("min_price")
            )
            .where(RoomVariant.is_active == True, RoomVariant.deleted_at == None)
            .group_by(RoomVariant.room_id)
            .subquery()
        )

        # Fetch Data
        data_query = (
            base_query
            .outerjoin(price_subquery, Room.id == price_subquery.c.room_id)
            .options(selectinload(Room.variants.and_(RoomVariant.is_active == True, RoomVariant.deleted_at == None)))
        )

        # Sorting
        if sort == "price_low":
            data_query = data_query.order_by(price_subquery.c.min_price.asc().nulls_last(), Room.id.desc())
        elif sort == "price_high":
            data_query = data_query.order_by(price_subquery.c.min_price.desc().nulls_last(), Room.id.desc())
        else: # Default: priority
            data_query = data_query.order_by(Room.order_priority.asc(), Room.id.desc())

        data_query = data_query.offset(offset).limit(size)
        
        result = await db.execute(data_query)
        rooms = result.scalars().all()

        # Map to DTOs
        dto_list = []
        for r in rooms:
            starting_price = min((v.weekday_price for v in r.variants), default=None)
            
            dto_list.append(RoomListDTO(
                id=r.id,
                slug=r.slug,
                lodge_name=r.lodge_name,
                cover_image_url=r.cover_image_url,
                is_featured=r.is_featured,
                starting_price=starting_price,
                address=r.address,
                map_url=r.map_url,
                facilities=r.facilities if r.facilities else []
            ))

        has_next = (offset + size) < total_count
        has_prev = page > 1

        return PaginatedResponse(
            items=dto_list,
            total=total_count,
            page=page,
            size=size,
            has_next=has_next,
            has_prev=has_prev
        )

    return await ttl_cache_get_or_set(cache_key, PUBLIC_CACHE_TTL_SECONDS, load_rooms)

@router.get("/{slug}", response_model=RoomDetailDTO)
async def get_room_detail(
    slug: str,
    response: Response,
    db: AsyncSession = Depends(get_db)
):
    """
    Public Room Detail API.
    Returns full details for a specific room including rich content.
    """
    cache_key = f"rooms:detail:{slug}"
    set_public_cache_headers(response)

    async def load_room_detail() -> RoomDetailDTO:
        query = (
            select(Room)
            .where(
                Room.slug == slug,
                Room.status == PublishStatus.PUBLISHED,
                Room.is_active == True,
                Room.deleted_at.is_(None)
            )
            .options(
                selectinload(Room.variants.and_(RoomVariant.is_active == True, RoomVariant.deleted_at == None)),
                selectinload(Room.gallery),
                selectinload(Room.highlights),
                selectinload(Room.faqs),
                selectinload(Room.policies)
            )
        )
        
        result = await db.execute(query)
        r = result.scalar_one_or_none()
        
        if not r:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room not found or inactive"
            )
            
        starting_price = min((v.weekday_price for v in r.variants), default=None)
        
        from app.services.r2_storage import r2_service
        brochure_url = await r2_service.get_public_url(r.brochure_pdf_url or r.generated_brochure_url)
        gen_brochure_url = await r2_service.get_public_url(r.generated_brochure_url)
        
        return RoomDetailDTO(
            id=r.id,
            slug=r.slug,
            lodge_name=r.lodge_name,
            cover_image_url=r.cover_image_url,
            is_featured=r.is_featured,
            starting_price=starting_price,
            address=r.address,
            map_url=r.map_url,
            facilities=r.facilities if r.facilities else [],
            description=r.description,
            brochure_pdf_url=brochure_url,
            generated_brochure_url=gen_brochure_url,
            total_rooms=r.total_rooms,
            slot_start=r.slot_start,
            slot_end=r.slot_end,
            booking_slots=r.booking_slots if r.booking_slots else [],
            created_at=r.created_at,
            updated_at=r.updated_at,
            meta_title=r.meta_title,
            meta_description=r.meta_description,
            og_image_url=r.og_image_url,
            canonical_url=r.canonical_url,
            variants=[
                RoomVariantPublicDTO(
                    id=v.id,
                    variant_name=v.variant_name,
                    weekday_price=v.weekday_price,
                    weekend_price=v.weekend_price,
                    capacity_per_room=v.capacity_per_room
                ) for v in r.variants
            ],
            gallery=r.gallery,
            highlights=r.highlights,
            faqs=r.faqs,
            policies=r.policies
        )

    return await ttl_cache_get_or_set(cache_key, PUBLIC_CACHE_TTL_SECONDS, load_room_detail)
