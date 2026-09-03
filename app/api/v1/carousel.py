"""
Public Homepage Carousel API.

Returns the featured packages and rooms that have is_featured=True.
These are displayed as dynamic slides in the homepage hero section.
Each slide contains a cover image, title, description excerpt, starting price,
and a direct "Book Now" link (slug).
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from decimal import Decimal

from app.db.session import get_db
from app.models.package import Package
from app.models.room import Room
from app.models.enums import PublishStatus
from app.utils.cache import set_public_cache_headers, ttl_cache_get_or_set

router = APIRouter()
CAROUSEL_CACHE_TTL = 60  # 60 seconds


class CarouselSlide(BaseModel):
    type: str              # "package" | "room"
    slug: str
    title: str
    description: Optional[str] = None
    cover_image_url: Optional[str] = None
    starting_price: Optional[Decimal] = None
    region: Optional[str] = None
    duration: Optional[str] = None
    place: Optional[str] = None
    # For rooms
    address: Optional[str] = None
    package_type: Optional[str] = None
    starting_weekend_price: Optional[Decimal] = None
    child_price: Optional[Decimal] = None
    # For students
    is_student_package: bool = False
    student_price: Optional[Decimal] = None
    weekend_student_price: Optional[Decimal] = None
    refreshment_student_price: Optional[Decimal] = None
    has_refreshments: bool = False


@router.get("/carousel", response_model=List[CarouselSlide], tags=["Public Discovery - Carousel"])
async def get_carousel_slides(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all published, featured packages and rooms for the homepage carousel.
    Items are ordered by order_priority (highest first), then by id descending.
    The carousel will show all slides added by the administrator.
    """
    from app.core.memory_cache import get_mem_cached, set_mem_cached
    set_public_cache_headers(response)

    cached = get_mem_cached("carousel", "slides")
    if cached is not None:
        return cached

    slides: List[CarouselSlide] = []

    import html
    import re

    # Fetch featured packages (PUBLISHED + is_featured=True)
    pkg_result = await db.execute(
        select(
            Package.id,
            Package.slug,
            Package.title,
            Package.description,
            Package.cover_image_url,
            Package.starting_price,
            Package.region,
            Package.duration,
            Package.place,
            Package.type,
            Package.is_student_package,
            Package.refreshment_student_price,
            Package.has_refreshments,
        )
        .where(
            Package.status == PublishStatus.PUBLISHED,
            Package.is_featured == True,
            Package.deleted_at.is_(None),
        )
        .order_by(Package.order_priority.desc(), Package.id.desc())
    )
    packages = pkg_result.all()

    # Batch fetch all variants in ONE query instead of N+1
    pkg_ids = [p.id for p in packages]
    var_map: dict[int, list] = {}
    if pkg_ids:
        from app.models.package import PackageVariant
        var_result = await db.execute(
            select(
                PackageVariant.package_id,
                PackageVariant.child_price,
                PackageVariant.student_price,
                PackageVariant.weekend_student_price
            )
            .where(
                PackageVariant.package_id.in_(pkg_ids),
                PackageVariant.is_active == True,
                PackageVariant.deleted_at.is_(None),
            )
        )
        for v_pkg_id, c_price, s_price, wk_s_price in var_result.all():
            var_map.setdefault(v_pkg_id, []).append((c_price, s_price, wk_s_price))

    for pkg in packages:
        desc = pkg.description or ""
        desc = html.unescape(desc)
        desc = re.sub(r'<[^>]+>', '', desc).strip()
        desc = desc[:180] + "..." if len(desc) > 180 else desc
        title = html.unescape(pkg.title or "")

        v_list = var_map.get(pkg.id, [])
        child_prices = [row[0] for row in v_list if row[0] is not None]
        min_child = min(child_prices) if child_prices else None

        student_prices = [row[1] for row in v_list if row[1] is not None]
        min_student = min(student_prices) if student_prices else None

        wk_student_prices = [row[2] for row in v_list if row[2] is not None]
        min_wk_student = min(wk_student_prices) if wk_student_prices else None

        slides.append(CarouselSlide(
            type="package",
            slug=pkg.slug,
            title=title,
            description=desc or None,
            cover_image_url=pkg.cover_image_url,
            starting_price=pkg.starting_price,
            region=str(pkg.region.value) if pkg.region else None,
            duration=pkg.duration,
            place=pkg.place,
            package_type=pkg.type.value if pkg.type else None,
            child_price=min_child,
            is_student_package=pkg.is_student_package,
            student_price=min_student,
            weekend_student_price=min_wk_student,
            refreshment_student_price=pkg.refreshment_student_price,
            has_refreshments=pkg.has_refreshments,
        ))

    # Fetch featured rooms (PUBLISHED + is_featured=True)
    room_result = await db.execute(
        select(
            Room.slug,
            Room.lodge_name,
            Room.description,
            Room.cover_image_url,
            Room.starting_price,
            Room.starting_weekend_price,
            Room.address,
        )
        .where(
            Room.status == PublishStatus.PUBLISHED,
            Room.is_featured == True,
            Room.is_active == True,
            Room.deleted_at.is_(None),
        )
        .order_by(Room.order_priority.desc(), Room.id.desc())
    )
    rooms = room_result.all()

    for room in rooms:
        desc = room.description or ""
        desc = html.unescape(desc)
        desc = re.sub(r'<[^>]+>', '', desc).strip()
        desc = desc[:180] + "..." if len(desc) > 180 else desc
        title = html.unescape(room.lodge_name or "")

        slides.append(CarouselSlide(
            type="room",
            slug=room.slug,
            title=title,
            description=desc or None,
            cover_image_url=room.cover_image_url,
            starting_price=room.starting_price,
            starting_weekend_price=room.starting_weekend_price,
            address=room.address,
        ))

    set_mem_cached("carousel", "slides", slides, ttl_seconds=CAROUSEL_CACHE_TTL)
    return slides

