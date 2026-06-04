from typing import Optional, List
from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_, text
from sqlalchemy.orm import selectinload, joinedload

from app.core.timezone import ist_date_today
from app.db.session import get_db
from app.models.package import Package, PackageVariant, PackageVariantInventory, package_tags
from app.models.tag import Tag
from app.models.enums import PackageType, RegionType, PublishStatus
from app.schemas.public import PaginatedResponse, PackageListDTO, PackageDetailDTO, PackageVariantPublicDTO, TransportOptionPublicDTO
from app.schemas.inventory import PublicDateAvailability, PublicPackageAvailabilityResponse
from app.utils.cache import set_no_store_headers, set_public_cache_headers, ttl_cache_get_or_set
from app.utils.pricing import get_effective_package_prices

router = APIRouter()
PUBLIC_CACHE_TTL_SECONDS = 60

def get_active_paid_variants(pkg: Package) -> list[PackageVariant]:
    return [
        v for v in pkg.variants
        if v.is_active and not v.deleted_at and v.adult_price and v.adult_price > 0
    ]

def has_text(value: object) -> bool:
    return bool(str(value or "").strip())

@router.get("", response_model=PaginatedResponse[PackageListDTO])
async def get_packages(
    response: Response,
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(10, ge=1, le=100, description="Items per page"),
    type: Optional[PackageType] = Query(None, description="Filter by TOUR or TRIP"),
    region: Optional[RegionType] = Query(None, description="Filter by AP or TS"),
    is_featured: Optional[bool] = Query(None, description="Filter featured only"),
    tags: Optional[List[str]] = Query(None, description="Filter by tags"),
    place: Optional[str] = Query(None, description="Filter by place"),
    sort: Optional[str] = Query("priority", description="Sort by: priority, price_low, price_high"),
    q: Optional[str] = Query(None, description="Search term for title/description")
):
    """
    Public Package Discovery API.
    Returns a paginated list of active packages, properly eagerly loading variants for starting_price.
    """
    cache_key = f"packages:list:{page}:{size}:{type}:{region}:{is_featured}:{tuple(tags or [])}:{place or ''}:{sort}:{q or ''}"
    set_public_cache_headers(response)

    async def load_packages() -> PaginatedResponse[PackageListDTO]:
        offset = (page - 1) * size

        # Base Query (Only PUBLISHED packages)
        base_query = select(Package).where(
            Package.status == PublishStatus.PUBLISHED,
            Package.deleted_at.is_(None)
        )

        # Filters
        if type:
            base_query = base_query.where(Package.type == type)
        if region:
            base_query = base_query.where(Package.region == region)
        if is_featured is not None:
            base_query = base_query.where(Package.is_featured == is_featured)
        if place:
            base_query = base_query.where(
                or_(
                    Package.place == place,
                    Package.tags.any(Tag.name.ilike(f"%{place}%"))
                )
            )
        
        if q:
            fts_vector = func.to_tsvector(text("'english'::regconfig"), Package.title + ' ' + func.coalesce(Package.description, ''))
            base_query = base_query.where(
                fts_vector.op('@@')(func.websearch_to_tsquery(text("'english'::regconfig"), q))
            )
        
        if tags:
            # Filter packages that have ANY of the requested tags (OR logic)
            base_query = base_query.where(Package.tags.any(Tag.name.in_(tags)))

        # Count Query
        count_query = base_query.with_only_columns(func.count()).order_by(None)

        # Projection Query to avoid ORM Hydration overhead
        data_query = (
            base_query
            .outerjoin(package_tags, Package.id == package_tags.c.package_id)
            .outerjoin(Tag, package_tags.c.tag_id == Tag.id)
            .with_only_columns(
                Package.id,
                Package.slug,
                Package.title,
                Package.type,
                Package.duration,
                Package.place,
                Package.region,
                Package.cover_image_url,
                Package.brochure_pdf_url,
                Package.generated_brochure_url,
                Package.is_featured,
                Package.starting_price,
                func.array_remove(func.array_agg(func.distinct(Tag.name)), None).label("tags_list")
            )
            .group_by(Package.id)
        )

        # Sorting
        if sort == "price_low":
            data_query = data_query.order_by(Package.starting_price.asc().nulls_last(), Package.id.desc())
        elif sort == "price_high":
            data_query = data_query.order_by(Package.starting_price.desc().nulls_last(), Package.id.desc())
        else: # Default: priority
            data_query = data_query.order_by(Package.order_priority.asc(), Package.id.desc())

        data_query = data_query.offset(offset).limit(size)
        
        total_count = (await db.execute(count_query)).scalar_one()
        packages = (await db.execute(data_query)).all()

        # Fetch variants manually in one go to avoid ORM hydration penalty while giving frontend child pricing
        package_ids = [pkg.id for pkg in packages]
        variants_by_pkg = {}
        if package_ids:
            from app.models.package import PackageVariant
            variants_query = select(PackageVariant).where(
                PackageVariant.package_id.in_(package_ids),
                PackageVariant.is_active == True,
                PackageVariant.deleted_at.is_(None)
            )
            all_variants = (await db.execute(variants_query)).scalars().all()
            for v in all_variants:
                variants_by_pkg.setdefault(v.package_id, []).append(PackageVariantPublicDTO(
                    id=v.id,
                    title=v.title,
                    adult_price=v.adult_price,
                    child_price=v.child_price,
                    transport_info=None
                ))

        # Map to DTOs
        from app.services.r2_storage import r2_service
        import asyncio

        async def build_dto(pkg):
            active_brochure_key = pkg.generated_brochure_url or pkg.brochure_pdf_url
            brochure_url, gen_brochure_url = await asyncio.gather(
                r2_service.get_public_url(active_brochure_key),
                r2_service.get_public_url(pkg.generated_brochure_url)
            )
            return PackageListDTO(
                id=pkg.id,
                slug=pkg.slug,
                title=pkg.title,
                type=pkg.type,
                duration=pkg.duration,
                place=pkg.place,
                region=pkg.region,
                brochure_pdf_url=brochure_url,
                generated_brochure_url=gen_brochure_url,
                cover_image_url=pkg.cover_image_url,
                is_featured=pkg.is_featured,
                tags=pkg.tags_list or [],
                starting_price=pkg.starting_price,
                transport_info=None,
                variants=variants_by_pkg.get(pkg.id, [])
            )

        dto_list = await asyncio.gather(*(build_dto(pkg) for pkg in packages))

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

    return await ttl_cache_get_or_set(cache_key, PUBLIC_CACHE_TTL_SECONDS, load_packages)

@router.get("/{slug}", response_model=PackageDetailDTO)
async def get_package_detail(
    slug: str,
    response: Response,
    db: AsyncSession = Depends(get_db)
):
    """
    Public Package Detail API.
    Returns full details for a specific package, including all content sections.
    """
    cache_key = f"packages:detail:{slug}"
    set_public_cache_headers(response)

    async def load_package_detail() -> PackageDetailDTO:
        query = (
            select(Package)
            .where(
                Package.slug == slug,
                Package.status == PublishStatus.PUBLISHED,
                Package.deleted_at.is_(None)
            )
        )
        
        pkg = (await db.execute(query)).unique().scalar_one_or_none()
        
        if not pkg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Package not found or inactive"
            )

        import asyncio
        from app.models.package import PackageVariant, PackageGalleryImage, PackageItineraryDay, PackageHighlight, PackageInclusion, PackageExclusion, PackageBoardingPoint, PackageFAQ, PackagePolicy, PackageTransportOption

        # Fetch all relationships concurrently to eliminate sequential roundtrips
        async def fetch_rel(model, active_filter=None):
            q = select(model).where(model.package_id == pkg.id)
            if active_filter is not None:
                q = q.where(active_filter)
            return (await db.execute(q)).scalars().all()

        results = await asyncio.gather(
            fetch_rel(PackageVariant, and_(PackageVariant.is_active == True, PackageVariant.deleted_at == None)),
            fetch_rel(PackageGalleryImage),
            fetch_rel(PackageItineraryDay),
            fetch_rel(PackageHighlight),
            fetch_rel(PackageInclusion),
            fetch_rel(PackageExclusion),
            fetch_rel(PackageBoardingPoint),
            fetch_rel(PackageFAQ),
            fetch_rel(PackagePolicy),
            fetch_rel(PackageTransportOption),
        )

        pkg_variants = results[0]
        # For tags, we must join the association table
        tags_query = select(Tag).join(package_tags).where(package_tags.c.package_id == pkg.id, Tag.is_active == True)
        pkg_tags = (await db.execute(tags_query)).scalars().all()
        
        pkg_gallery = results[1]
        pkg_itinerary = results[2]
        pkg_highlights = results[3]
        pkg_inclusions = results[4]
        pkg_exclusions = results[5]
        pkg_boarding_points = results[6]
        pkg_faqs = results[7]
        pkg_policies = results[8]
        pkg_transport_options = results[9]
            
        starting_price = min((v.adult_price for v in pkg_variants), default=None)
        
        from app.services.r2_storage import r2_service
        active_brochure_key = pkg.generated_brochure_url or pkg.brochure_pdf_url
        brochure_url = await r2_service.get_public_url(active_brochure_key)
        gen_brochure_url = await r2_service.get_public_url(pkg.generated_brochure_url)
        
        return PackageDetailDTO(
            id=pkg.id,
            slug=pkg.slug,
            title=pkg.title,
            type=pkg.type,
            duration=pkg.duration,
            place=pkg.place,
            region=pkg.region,
            description=pkg.description,
            brochure_pdf_url=brochure_url,
            generated_brochure_url=gen_brochure_url,
            cover_image_url=pkg.cover_image_url,
            is_featured=pkg.is_featured,
            tags=[tag.name for tag in pkg_tags if tag.is_active],
            starting_price=starting_price,
            # Transport & Refreshments
            has_transport=pkg.has_transport or False,
            transport_options=[
                TransportOptionPublicDTO(
                    id=t.id,
                    type=t.type,
                    title=t.title,
                    capacity=t.capacity,
                    adult_price=t.adult_price,
                    child_price=t.child_price,
                    weekend_adult_price=t.weekend_adult_price,
                    weekend_child_price=t.weekend_child_price,
                    fixed_price=t.fixed_price,
                    weekend_fixed_price=t.weekend_fixed_price,
                ) for t in pkg_transport_options
            ],
            has_refreshments=pkg.has_refreshments or False,
            refreshment_adult_price=pkg.refreshment_adult_price,
            refreshment_child_price=pkg.refreshment_child_price,
            meta_title=pkg.meta_title,
            meta_description=pkg.meta_description,
            og_image_url=pkg.og_image_url,
            canonical_url=pkg.canonical_url,
            variants=[
                PackageVariantPublicDTO(
                    id=v.id,
                    title=v.title,
                    adult_price=v.adult_price,
                    child_price=v.child_price,
                    transport_info=None
                ) for v in pkg_variants
            ],
            gallery=[
                item for item in pkg_gallery
                if not item.deleted_at and has_text(item.image_url)
            ],
            itinerary=[
                item for item in pkg_itinerary
                if not item.deleted_at and has_text(item.title)
            ],
            highlights=[
                item for item in pkg_highlights
                if not item.deleted_at and has_text(item.title)
            ],
            inclusions=[
                item for item in pkg_inclusions
                if not item.deleted_at and has_text(item.label)
            ],
            exclusions=[
                item for item in pkg_exclusions
                if not item.deleted_at and has_text(item.label)
            ],
            boarding_points=[
                item for item in pkg_boarding_points
                if not item.deleted_at and has_text(item.title)
            ],
            faqs=[
                item for item in pkg_faqs
                if not item.deleted_at and has_text(item.question) and has_text(item.answer)
            ],
            policies=[
                item for item in pkg_policies
                if not item.deleted_at and has_text(item.title) and has_text(item.description)
            ]
        )

    return await ttl_cache_get_or_set(cache_key, PUBLIC_CACHE_TTL_SECONDS, load_package_detail)


@router.get("/{slug}/availability", response_model=PublicPackageAvailabilityResponse)
async def get_package_availability(
    slug: str,
    response: Response,
    month: str = Query(..., description="Month in YYYY-MM format, e.g. 2026-06"),
    db: AsyncSession = Depends(get_db),
):
    """
    Public availability endpoint for a package's detail page.
    Returns per-date seat counts, price overrides, and open/closed status
    for all active variants within the requested month.

    Business rules enforced:
    - Today and past dates are excluded from results (no same-day booking).
    - Dates with is_closed=True are returned with status='CLOSED'.
    - Dates with available_seats=0 are returned with status='SOLD_OUT'.
    - Dates with no inventory row are returned with status='NO_INVENTORY'.
    """
    set_no_store_headers(response)
    today = ist_date_today()

    # Check Redis cache first
    from app.services.redis_client import get_cached_availability
    cached = await get_cached_availability(slug, month)
    if cached is not None:
        return cached

    # Validate month format
    try:
        year, mon = int(month[:4]), int(month[5:7])
        from_date = date(year, mon, 1)
        if mon == 12:
            to_date = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            to_date = date(year, mon + 1, 1) - timedelta(days=1)
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format.")

    # Load package with variants
    pkg_result = await db.execute(
        select(Package)
        .where(
            Package.slug == slug,
            Package.status == PublishStatus.PUBLISHED,
            Package.deleted_at.is_(None),
        )
        .options(selectinload(Package.variants))
    )
    pkg = pkg_result.scalar_one_or_none()
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found or inactive.")

    active_variants = get_active_paid_variants(pkg)
    if not active_variants:
        res_data = PublicPackageAvailabilityResponse(
            package_id=pkg.id, slug=pkg.slug, month=month, dates=[]
        )
        from app.services.redis_client import set_cached_availability
        await set_cached_availability(slug, month, res_data.model_dump(), ttl_seconds=60)
        return res_data

    variant_ids = [v.id for v in active_variants]
    variant_map = {v.id: v for v in active_variants}

    # Fetch all inventory rows for these variants in the month
    inv_result = await db.execute(
        select(PackageVariantInventory).where(
            and_(
                PackageVariantInventory.variant_id.in_(variant_ids),
                PackageVariantInventory.date >= from_date,
                PackageVariantInventory.date <= to_date,
            )
        ).order_by(PackageVariantInventory.date.asc(), PackageVariantInventory.variant_id.asc())
    )
    inv_rows = inv_result.scalars().all()

    # Build a map: (variant_id, date) -> inventory_row
    inv_map: dict[tuple, PackageVariantInventory] = {}
    for row in inv_rows:
        inv_map[(row.variant_id, row.date)] = row

    availability: list[PublicDateAvailability] = []

    # Walk every date in the month, for every variant
    current = from_date
    while current <= to_date:
        # Skip past dates
        if current < today:
            current += timedelta(days=1)
            continue

        for variant in active_variants:
            inv = inv_map.get((variant.id, current))

            base_adult = variant.adult_price
            base_child = variant.child_price

            if inv is None:
                eff_adult, eff_child = get_effective_package_prices(base_adult, base_child, None)
                availability.append(
                    PublicDateAvailability(
                        date=current,
                        variant_id=variant.id,
                        variant_title=variant.title,
                        adult_price=base_adult,
                        child_price=base_child,
                        effective_adult_price=eff_adult,
                        effective_child_price=eff_child,
                        available_seats=0,
                        is_closed=False,
                        status="NO_INVENTORY",
                    )
                )
            elif inv.is_closed:
                eff_adult, eff_child = get_effective_package_prices(base_adult, base_child, inv.price_override)
                availability.append(
                    PublicDateAvailability(
                        date=current,
                        variant_id=variant.id,
                        variant_title=variant.title,
                        adult_price=base_adult,
                        child_price=base_child,
                        effective_adult_price=eff_adult,
                        effective_child_price=eff_child,
                        available_seats=max(0, inv.total_capacity - (inv.booked_count + inv.reserved_count)),
                        is_closed=True,
                        status="CLOSED",
                    )
                )
            else:
                eff_adult, eff_child = get_effective_package_prices(base_adult, base_child, inv.price_override)
                avail_seats = max(0, inv.total_capacity - (inv.booked_count + inv.reserved_count))
                slot_status = "OPEN" if avail_seats > 0 else "SOLD_OUT"
                availability.append(
                    PublicDateAvailability(
                        date=current,
                        variant_id=variant.id,
                        variant_title=variant.title,
                        adult_price=base_adult,
                        child_price=base_child,
                        effective_adult_price=eff_adult,
                        effective_child_price=eff_child,
                        available_seats=avail_seats,
                        is_closed=False,
                        status=slot_status,
                    )
                )

        current += timedelta(days=1)

    res_data = PublicPackageAvailabilityResponse(
        package_id=pkg.id,
        slug=pkg.slug,
        month=month,
        dates=availability,
    )
    from app.services.redis_client import set_cached_availability
    await set_cached_availability(slug, month, res_data.model_dump(), ttl_seconds=60)
    return res_data


@router.get("/places/all", response_model=List[str])
async def get_unique_places(db: AsyncSession = Depends(get_db)):
    """
    Returns a distinct list of all places dynamically configured in the active packages database,
    falling back to known place tags to bootstrap immediately.
    """
    # 1. Get explicit places from Package.place column
    result = await db.execute(
        select(Package.place)
        .where(
            Package.status == PublishStatus.PUBLISHED,
            Package.deleted_at.is_(None),
            Package.place.is_not(None),
            Package.place != ""
        )
        .distinct()
    )
    explicit_places = [row[0] for row in result.all() if row[0]]
    
    # 2. Add dynamic fallback/bootstrap place tags that actually exist in tags table
    bootstrap_names = ["Rajahmundry", "Bhadrachalam", "Papikondalu", "Kolluru"]
    tag_result = await db.execute(
        select(Tag.name)
        .where(Tag.name.in_(bootstrap_names))
    )
    existing_tags = [row[0] for row in tag_result.all() if row[0]]
    
    all_places = list(set(explicit_places + existing_tags))
    if not all_places:
        all_places = bootstrap_names
        
    return sorted(all_places)
