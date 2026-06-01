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


@router.get("/carousel", response_model=List[CarouselSlide], tags=["Public Discovery - Carousel"])
async def get_carousel_slides(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all published, featured packages and rooms for the homepage carousel.
    Items are ordered by order_priority (highest first), then by id descending.
    The carousel will show up to 6 total slides (first 3 packages + first 3 rooms).
    """
    cache_key = "carousel:homepage:slides"
    set_public_cache_headers(response)

    async def load_carousel() -> List[CarouselSlide]:
        slides: List[CarouselSlide] = []

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
            )
            .where(
                Package.status == PublishStatus.PUBLISHED,
                Package.is_featured == True,
                Package.deleted_at.is_(None),
            )
            .order_by(Package.order_priority.desc(), Package.id.desc())
            .limit(5)
        )
        packages = pkg_result.all()

        for pkg in packages:
            # Strip HTML from description and truncate
            desc = pkg.description or ""
            import re
            desc = re.sub(r'<[^>]+>', '', desc).strip()
            desc = desc[:180] + "..." if len(desc) > 180 else desc

            # Fetch variants to find the minimum child price
            from app.models.package import PackageVariant
            var_result = await db.execute(
                select(PackageVariant.child_price)
                .where(
                    PackageVariant.package_id == pkg.id,
                    PackageVariant.is_active == True,
                    PackageVariant.deleted_at.is_(None),
                )
            )
            child_prices = [row[0] for row in var_result.all() if row[0] is not None]
            min_child = min(child_prices) if child_prices else None

            slides.append(CarouselSlide(
                type="package",
                slug=pkg.slug,
                title=pkg.title,
                description=desc or None,
                cover_image_url=pkg.cover_image_url,
                starting_price=pkg.starting_price,
                region=str(pkg.region.value) if pkg.region else None,
                duration=pkg.duration,
                place=pkg.place,
                package_type=pkg.type.value if pkg.type else None,
                child_price=min_child,
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
            .limit(5)
        )
        rooms = room_result.all()

        for room in rooms:
            desc = room.description or ""
            import re
            desc = re.sub(r'<[^>]+>', '', desc).strip()
            desc = desc[:180] + "..." if len(desc) > 180 else desc

            slides.append(CarouselSlide(
                type="room",
                slug=room.slug,
                title=room.lodge_name,
                description=desc or None,
                cover_image_url=room.cover_image_url,
                starting_price=room.starting_price,
                starting_weekend_price=room.starting_weekend_price,
                address=room.address,
            ))

        return slides

    return await ttl_cache_get_or_set(cache_key, CAROUSEL_CACHE_TTL, load_carousel)
