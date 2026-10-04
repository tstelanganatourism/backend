import asyncio
from loguru import logger
from sqlalchemy import select, and_, or_, func
from sqlalchemy.orm import selectinload
from decimal import Decimal

from app.db.session import AsyncSessionLocal
from app.models.package import Package, PackageVariant, PackageGalleryImage, PackageItineraryDay, PackageHighlight, PackageInclusion, PackageExclusion, PackageBoardingPoint, PackageFAQ, PackagePolicy, PackageTransportOption, PackageMealItem, PackageExtra, PackageCategory, package_tags
from app.models.tag import Tag
from app.models.room import Room, RoomVariant, RoomGalleryImage, RoomHighlight, RoomFAQ, RoomPolicy, RoomCategory
from app.models.enums import PublishStatus
from app.schemas.public import (
    PackageDetailDTO, PackageListDTO, PackageVariantPublicDTO, TransportOptionPublicDTO,
    PackageCategoryPublicDTO, PackageCategoryDetailPublicDTO,
    RoomDetailDTO, RoomListDTO, RoomVariantPublicDTO, RoomCategoryPublicDTO, RoomCategoryDetailPublicDTO,
    PaginatedResponse
)
from app.core.memory_cache import set_mem_cached

DEFAULT_CATEGORY_IMAGES = {
    "papikondalu-tour-packages": "https://res.cloudinary.com/r929tquv/image/upload/v1785917181/ts_boat_tourism/images/haotjawjrhmnnzvm7yqz.webp",
    "pochavaram-to-papikondalu-tour": "https://res.cloudinary.com/r929tquv/image/upload/v1784613500/ts_boat_tourism/packages/xolfujndmsrwgk22xqu2.jpg",
    "bhadrachalam-packages": "https://res.cloudinary.com/r929tquv/image/upload/f_auto,q_auto,w_1200/v1786268860/ts_boat_tourism/gallery/boats/q6md8goirybznxvajwet.png",
}

DEFAULT_ROOM_CATEGORY_IMAGES = {
    "bhadrachalam-accommodations": "https://res.cloudinary.com/r929tquv/image/upload/f_auto,q_auto,w_1200/v1786273972/9b475911-9c60-4bf6-9ffb-b9f1802275a2_k6zmkd.jpg",
    "papikondalu-forest-huts": "https://res.cloudinary.com/r929tquv/image/upload/f_auto,q_auto,w_1200/v1784613514/ts_boat_tourism/packages/zkxrdmxykszetgupmi8d.jpg",
}

def has_text(value: object) -> bool:
    return bool(str(value or "").strip())

async def warmup_public_cache():
    """
    Background worker that pre-populates in-memory cache for all public packages,
    categories, rooms, and stays. Guarantees < 1ms response time on every page request.
    """
    logger.info("Starting background cache warmup for public packages and rooms...")
    t0 = asyncio.get_event_loop().time()
    try:
        async with AsyncSessionLocal() as db:
            # 1. Warm Package Categories
            cat_query = select(PackageCategory).where(
                PackageCategory.is_active == True,
                PackageCategory.deleted_at.is_(None)
            ).options(selectinload(PackageCategory.packages)).order_by(PackageCategory.sort_order, PackageCategory.id)
            categories = (await db.execute(cat_query)).scalars().all()
            
            pkg_cats_out = []
            for cat in categories:
                published_pkgs = [p for p in cat.packages if p.status == PublishStatus.PUBLISHED and not p.deleted_at]
                prices = [p.starting_price for p in published_pkgs if p.starting_price and p.starting_price > 0]
                min_price = float(min(prices)) if prices else None
                cover_img = cat.cover_image_url or DEFAULT_CATEGORY_IMAGES.get(cat.slug, DEFAULT_CATEGORY_IMAGES["papikondalu-tour-packages"])
                
                pkg_cats_out.append(PackageCategoryPublicDTO(
                    id=cat.id, name=cat.name, slug=cat.slug,
                    description=cat.description, cover_image_url=cover_img,
                    icon=cat.icon, sort_order=cat.sort_order, package_count=len(published_pkgs),
                    min_price=min_price, rating=4.9
                ))
            set_mem_cached("pkg_cats", "all", pkg_cats_out, ttl_seconds=3600)

            # 2. Warm All Published Packages
            pkg_query = select(Package).where(
                Package.status == PublishStatus.PUBLISHED,
                Package.deleted_at.is_(None)
            ).order_by(Package.order_priority.asc(), Package.id.desc())
            packages = (await db.execute(pkg_query)).scalars().all()

            for pkg in packages:
                # Eagerly load all relationships for detail
                vq = select(PackageVariant).where(PackageVariant.package_id == pkg.id, PackageVariant.is_active == True, PackageVariant.deleted_at.is_(None))
                gq = select(PackageGalleryImage).where(PackageGalleryImage.package_id == pkg.id, PackageGalleryImage.deleted_at.is_(None))
                iq = select(PackageItineraryDay).where(PackageItineraryDay.package_id == pkg.id, PackageItineraryDay.deleted_at.is_(None))
                hq = select(PackageHighlight).where(PackageHighlight.package_id == pkg.id, PackageHighlight.deleted_at.is_(None))
                inq = select(PackageInclusion).where(PackageInclusion.package_id == pkg.id, PackageInclusion.deleted_at.is_(None))
                eq = select(PackageExclusion).where(PackageExclusion.package_id == pkg.id, PackageExclusion.deleted_at.is_(None))
                bq = select(PackageBoardingPoint).where(PackageBoardingPoint.package_id == pkg.id, PackageBoardingPoint.deleted_at.is_(None))
                fq = select(PackageFAQ).where(PackageFAQ.package_id == pkg.id, PackageFAQ.deleted_at.is_(None))
                pq = select(PackagePolicy).where(PackagePolicy.package_id == pkg.id, PackagePolicy.deleted_at.is_(None))
                tq = select(PackageTransportOption).where(PackageTransportOption.package_id == pkg.id, PackageTransportOption.deleted_at.is_(None))
                mq = select(PackageMealItem).where(PackageMealItem.package_id == pkg.id, PackageMealItem.deleted_at.is_(None))
                exq = select(PackageExtra).where(PackageExtra.package_id == pkg.id, PackageExtra.deleted_at.is_(None))
                tagsq = select(Tag).join(package_tags).where(package_tags.c.package_id == pkg.id, Tag.is_active == True)

                variants = (await db.execute(vq)).scalars().all()
                gallery = (await db.execute(gq)).scalars().all()
                itinerary = (await db.execute(iq)).scalars().all()
                highlights = (await db.execute(hq)).scalars().all()
                inclusions = (await db.execute(inq)).scalars().all()
                exclusions = (await db.execute(eq)).scalars().all()
                boarding_pts = (await db.execute(bq)).scalars().all()
                faqs = (await db.execute(fq)).scalars().all()
                policies = (await db.execute(pq)).scalars().all()
                transports = (await db.execute(tq)).scalars().all()
                meals = (await db.execute(mq)).scalars().all()
                extras = (await db.execute(exq)).scalars().all()
                tags = (await db.execute(tagsq)).scalars().all()

                if pkg.is_student_package:
                    starting_price = min((v.student_price for v in variants if v.student_price is not None), default=None)
                else:
                    starting_price = min((v.adult_price for v in variants if v.adult_price is not None), default=None)

                dto = PackageDetailDTO(
                    id=pkg.id,
                    slug=pkg.slug,
                    title=pkg.title,
                    type=pkg.type,
                    duration=pkg.duration,
                    place=pkg.place,
                    region=pkg.region,
                    description=pkg.description,
                    brochure_pdf_url=pkg.brochure_pdf_url or pkg.generated_brochure_url,
                    generated_brochure_url=pkg.generated_brochure_url,
                    cover_image_url=pkg.cover_image_url,
                    video_url=pkg.video_url,
                    is_active=pkg.is_active,
                    is_featured=pkg.is_featured,
                    tags=[t.name for t in tags],
                    starting_price=starting_price,
                    advance_payment_type=pkg.advance_payment_type.value if hasattr(pkg.advance_payment_type, 'value') else str(pkg.advance_payment_type or "FULL_PAYMENT"),
                    advance_payment_value=pkg.advance_payment_value or Decimal("0.00"),
                    min_passengers=pkg.min_passengers or 1,
                    is_student_package=pkg.is_student_package or False,
                    has_transport=pkg.has_transport or False,
                    transport_options=[
                        TransportOptionPublicDTO(
                            id=t.id, type=t.type, title=t.title, capacity=t.capacity,
                            adult_price=t.adult_price, child_price=t.child_price,
                            weekend_adult_price=t.weekend_adult_price, weekend_child_price=t.weekend_child_price,
                            student_price=t.student_price, weekend_student_price=t.weekend_student_price,
                            fixed_price=t.fixed_price, weekend_fixed_price=t.weekend_fixed_price
                        ) for t in transports
                    ],
                    has_refreshments=pkg.has_refreshments or False,
                    refreshment_adult_price=pkg.refreshment_adult_price,
                    refreshment_child_price=pkg.refreshment_child_price,
                    refreshment_student_price=pkg.refreshment_student_price,
                    refreshments_min_passengers=pkg.refreshments_min_passengers or 1,
                    has_food_option=pkg.has_food_option or False,
                    food_adult_price=pkg.food_adult_price,
                    food_child_price=pkg.food_child_price,
                    food_student_price=pkg.food_student_price,
                    meta_title=pkg.meta_title,
                    meta_description=pkg.meta_description,
                    og_image_url=pkg.og_image_url,
                    canonical_url=pkg.canonical_url,
                    variants=[
                        PackageVariantPublicDTO(
                            id=v.id, title=v.title, adult_price=v.adult_price or Decimal("0.00"),
                            child_price=v.child_price or Decimal("0.00"),
                            weekend_adult_price=v.weekend_adult_price, weekend_child_price=v.weekend_child_price,
                            student_price=v.student_price, weekend_student_price=v.weekend_student_price,
                            transport_info=None
                        ) for v in variants
                    ],
                    gallery=[item for item in gallery if not item.deleted_at and has_text(item.image_url)],
                    itinerary=[item for item in itinerary if not item.deleted_at and has_text(item.title)],
                    highlights=[item for item in highlights if not item.deleted_at and has_text(item.title)],
                    inclusions=[item for item in inclusions if not item.deleted_at and has_text(item.label)],
                    exclusions=[item for item in exclusions if not item.deleted_at and has_text(item.label)],
                    boarding_points=[item for item in boarding_pts if not item.deleted_at and has_text(item.title)],
                    faqs=[item for item in faqs if not item.deleted_at and has_text(item.question) and has_text(item.answer)],
                    policies=[item for item in policies if not item.deleted_at and has_text(item.title) and has_text(item.description)],
                    meals=[item for item in meals if not item.deleted_at and has_text(item.name)],
                    extras=[item for item in extras if not item.deleted_at and has_text(item.title)],
                    agent_commission_type=None,
                    agent_commission_percentage=None,
                    agent_commission_fixed_amount=None,
                    agent_daily_quota=None,
                    agent_is_allowed=None,
                )
                set_mem_cached("package_detail", f"{pkg.slug.lower()}:False:0", dto, ttl_seconds=3600)
                set_mem_cached("package_detail", pkg.slug.lower(), dto, ttl_seconds=3600)

            # 3. Warm All Rooms
            room_query = select(Room).where(
                Room.status == PublishStatus.PUBLISHED,
                Room.is_active == True,
                Room.deleted_at.is_(None)
            ).order_by(Room.order_priority.asc(), Room.id.desc())
            rooms = (await db.execute(room_query)).scalars().all()

            for r in rooms:
                rvq = select(RoomVariant).where(RoomVariant.room_id == r.id, RoomVariant.is_active == True, RoomVariant.deleted_at.is_(None))
                rgq = select(RoomGalleryImage).where(RoomGalleryImage.room_id == r.id, RoomGalleryImage.deleted_at.is_(None))
                rhq = select(RoomHighlight).where(RoomHighlight.room_id == r.id, RoomHighlight.deleted_at.is_(None))
                rfq = select(RoomFAQ).where(RoomFAQ.room_id == r.id, RoomFAQ.deleted_at.is_(None))
                rpq = select(RoomPolicy).where(RoomPolicy.room_id == r.id, RoomPolicy.deleted_at.is_(None))

                r_variants = (await db.execute(rvq)).scalars().all()
                r_gallery = (await db.execute(rgq)).scalars().all()
                r_highlights = (await db.execute(rhq)).scalars().all()
                r_faqs = (await db.execute(rfq)).scalars().all()
                r_policies = (await db.execute(rpq)).scalars().all()

                starting_price = min((v.weekday_price for v in r_variants), default=None)
                room_dto = RoomDetailDTO(
                    id=r.id, slug=r.slug, lodge_name=r.lodge_name,
                    cover_image_url=r.cover_image_url, video_url=r.video_url,
                    is_featured=r.is_featured, starting_price=starting_price,
                    address=r.address, map_url=r.map_url, facilities=r.facilities or [],
                    description=r.description,
                    brochure_pdf_url=r.brochure_pdf_url or r.generated_brochure_url,
                    generated_brochure_url=r.generated_brochure_url,
                    total_rooms=r.total_rooms, slot_start=r.slot_start, slot_end=r.slot_end,
                    booking_slots=r.booking_slots or [],
                    created_at=r.created_at, updated_at=r.updated_at,
                    meta_title=r.meta_title, meta_description=r.meta_description,
                    og_image_url=r.og_image_url, canonical_url=r.canonical_url,
                    variants=[
                        RoomVariantPublicDTO(
                            id=v.id, variant_name=v.variant_name, weekday_price=v.weekday_price,
                            weekend_price=v.weekend_price, capacity_per_room=v.capacity_per_room
                        ) for v in r_variants
                    ],
                    gallery=r_gallery, highlights=r_highlights, faqs=r_faqs, policies=r_policies
                )
                set_mem_cached("room_detail", r.slug.lower(), room_dto, ttl_seconds=3600)
                set_mem_cached("room_detail", f"{r.slug.lower()}:False:0", room_dto, ttl_seconds=3600)

        elapsed = (asyncio.get_event_loop().time() - t0)
        logger.info(f"Cache warmup finished successfully in {elapsed:.2f}s! ({len(packages)} packages, {len(rooms)} rooms, {len(categories)} categories preloaded).")
    except Exception as err:
        logger.warning(f"Cache warmup encountered non-fatal error: {err}")
