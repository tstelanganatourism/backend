from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, text, delete
from typing import List, Optional
from pathlib import Path
import uuid
import logging
from app.core.config import settings
from app.db.session import get_db
from app.models.package import (
    Package,
    PackageVariant,
    PackageGalleryImage,
    PackageItineraryDay,
    PackageHighlight,
    PackageInclusion,
    PackageExclusion,
    PackageBoardingPoint,
    PackageFAQ,
    PackagePolicy,
    PackageTransportOption,
    PackageMealItem,
    PackageExtra,
    PackageCategory,
    package_category_assignments,
)
from app.schemas.package import PackageCreate, PackageUpdate, PackageDetailResponse, PackageResponse, PackagePaginatedResponse, PackageCategoryCreate, PackageCategoryUpdate, PackageCategoryResponse, PackageCategoryDetailResponse, PackageCategoryAssignRequest
from app.middleware.auth import require_admin
from app.models.user import User
from app.utils.audit import log_action
from app.utils.cache import clear_cache_prefix, clear_cache_prefix_async
import re

from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/packages",
    tags=["Admin - Package CMS"],
    dependencies=[Depends(require_admin)]
)

@router.post("/upload-image")
async def upload_admin_image(
    file: UploadFile = File(...),
    current_admin: User = Depends(require_admin)
):
    """Upload an image file to Cloudinary (or local static fallback) and return secure URL."""
    allowed_types = ["image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"]
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Only JPG, PNG, WEBP, and GIF images are allowed.")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty image file.")

    if settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET:
        try:
            import cloudinary
            import cloudinary.uploader
            cloudinary.config(
                cloud_name=settings.CLOUDINARY_CLOUD_NAME,
                api_key=settings.CLOUDINARY_API_KEY,
                api_secret=settings.CLOUDINARY_API_SECRET,
                secure=True
            )
            upload_result = cloudinary.uploader.upload(
                contents,
                folder="ts_boat_tourism/cms",
                resource_type="image"
            )
            return {"url": upload_result.get("secure_url")}
        except Exception as e:
            logger.warning(f"Cloudinary upload failed: {e}")

    # Local fallback
    upload_dir = Path("app/static/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"cms_{uuid.uuid4().hex[:8]}_{file.filename}"
    filepath = upload_dir / filename
    with open(filepath, "wb") as f:
        f.write(contents)
    return {"url": f"/static/uploads/{filename}"}

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text

async def sync_nested_relation(db: AsyncSession, package: Package, relation_name: str, model_class, input_data_list):
    """
    Syncs a nested one-to-many relationship using in-place list modification.
    - If a item in input_data_list has 'id', it will update the existing record.
    - If a item does not have 'id', it will create a new record.
    - Any existing record not present in input_data_list is deleted (via orphan removal).
    """
    current_list = getattr(package, relation_name)
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
            
    # Set the list in-place to trigger orphan cleanup
    setattr(package, relation_name, new_list)

@router.get("", response_model=PackagePaginatedResponse)
async def list_packages(
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db)
):
    """List all non-deleted packages with optional search and status filtering."""
    base_query = select(Package).where(Package.deleted_at.is_(None))
    
    if search:
        fts_vector = func.to_tsvector(text("'english'::regconfig"), Package.title + ' ' + func.coalesce(Package.description, ''))
        base_query = base_query.where(
            fts_vector.op('@@')(func.websearch_to_tsquery(text("'english'::regconfig"), search))
        )
        
    if status_filter:
        base_query = base_query.where(Package.status == status_filter)
        
    count_query = base_query.with_only_columns(func.count()).order_by(None)
    total_result = await db.execute(count_query)
    total_count = total_result.scalar_one()

    query = base_query.options(
        selectinload(Package.tags),
        selectinload(Package.categories),
        selectinload(Package.variants),
        selectinload(Package.transport_options)
    )
    query = query.order_by(Package.order_priority.desc(), Package.created_at.desc()).limit(limit).offset(offset)
    
    result = await db.execute(query)
    items = result.scalars().all()
    
    package_ids = [item.id for item in items]
    if package_ids:
        from app.models.booking import Booking
        from app.models.enums import BookingStatus
        booking_counts = await db.execute(
            select(PackageVariant.package_id, func.count(Booking.id))
            .join(Booking, Booking.variant_id == PackageVariant.id)
            .where(PackageVariant.package_id.in_(package_ids))
            .where(Booking.status.not_in([BookingStatus.CANCELLED, BookingStatus.REFUNDED]))
            .group_by(PackageVariant.package_id)
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

def full_package_options():
    return (
        selectinload(Package.tags),
        selectinload(Package.categories),
        selectinload(Package.variants),
        selectinload(Package.transport_options),
        selectinload(Package.gallery),
        selectinload(Package.itinerary),
        selectinload(Package.highlights),
        selectinload(Package.inclusions),
        selectinload(Package.exclusions),
        selectinload(Package.boarding_points),
        selectinload(Package.faqs),
        selectinload(Package.policies),
        selectinload(Package.meals),
        selectinload(Package.extras),
    )


# ── Admin Package Categories Sub-Router (MUST be before /{package_id}) ──────────

category_router = APIRouter(
    prefix="/categories",
    tags=["Admin - Package Categories"],
    dependencies=[Depends(require_admin)]
)

@category_router.get("", response_model=List[PackageCategoryDetailResponse])
async def list_package_categories(db: AsyncSession = Depends(get_db)):
    """List all package categories with their packages."""
    result = await db.execute(
        select(PackageCategory)
        .where(PackageCategory.deleted_at.is_(None))
        .options(selectinload(PackageCategory.packages).selectinload(Package.variants))
        .order_by(PackageCategory.sort_order, PackageCategory.id)
    )
    categories = result.scalars().all()
    return [
        PackageCategoryDetailResponse(
            id=cat.id,
            name=cat.name,
            slug=cat.slug,
            description=cat.description,
            cover_image_url=cat.cover_image_url,
            icon=cat.icon,
            sort_order=cat.sort_order,
            is_active=cat.is_active,
            package_count=len([p for p in cat.packages if not p.deleted_at]),
            packages=[PackageResponse(
                id=p.id,
                title=p.title,
                slug=p.slug,
                type=p.type,
                status=p.status,
                is_active=p.is_active,
                is_featured=p.is_featured,
                region=p.region,
                place=p.place,
                duration=p.duration,
                cover_image_url=p.cover_image_url,
                starting_price=p.starting_price,
                order_priority=p.order_priority,
                created_at=p.created_at,
                updated_at=p.updated_at,
                variants=[],
                tags=[],
            ) for p in cat.packages if not p.deleted_at],
        )
        for cat in categories
    ]

@category_router.post("", response_model=PackageCategoryResponse, status_code=201)
async def create_package_category(
    body: PackageCategoryCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new package category."""
    slug = body.slug or slugify(body.name)
    existing = await db.execute(select(PackageCategory).where(PackageCategory.slug == slug, PackageCategory.deleted_at.is_(None)))
    if existing.scalar_one_or_none():
        import time as _time
        slug = f"{slug.split('-')[0]}-{int(_time.time())}"
    cat = PackageCategory(
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
    await clear_cache_prefix_async("packages:")
    return PackageCategoryResponse(
        id=cat.id, name=cat.name, slug=cat.slug,
        description=cat.description, cover_image_url=cat.cover_image_url,
        icon=cat.icon, sort_order=cat.sort_order, is_active=cat.is_active,
        package_count=0,
    )

@category_router.patch("/{category_id}", response_model=PackageCategoryResponse)
async def update_package_category(
    category_id: int,
    body: PackageCategoryUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update a package category."""
    result = await db.execute(select(PackageCategory).where(PackageCategory.id == category_id, PackageCategory.deleted_at.is_(None)))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    for key, val in body.model_dump(exclude_none=True).items():
        setattr(cat, key, val)
    await db.commit()
    await db.refresh(cat)
    await clear_cache_prefix_async("packages:")
    return PackageCategoryResponse(
        id=cat.id, name=cat.name, slug=cat.slug,
        description=cat.description, cover_image_url=cat.cover_image_url,
        icon=cat.icon, sort_order=cat.sort_order, is_active=cat.is_active,
        package_count=0,
    )

@category_router.delete("/{category_id}", status_code=204)
async def delete_package_category(
    category_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Soft-delete a package category."""
    result = await db.execute(select(PackageCategory).where(PackageCategory.id == category_id, PackageCategory.deleted_at.is_(None)))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    from datetime import datetime, timezone
    cat.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    await clear_cache_prefix_async("packages:")

@category_router.post("/{category_id}/packages", status_code=200)
async def assign_packages_to_category(
    category_id: int,
    body: PackageCategoryAssignRequest,
    db: AsyncSession = Depends(get_db)
):
    """Assign (add) packages to a category."""
    result = await db.execute(select(PackageCategory).where(PackageCategory.id == category_id, PackageCategory.deleted_at.is_(None)).options(selectinload(PackageCategory.packages)))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    pkgs_result = await db.execute(select(Package).where(Package.id.in_(body.package_ids), Package.deleted_at.is_(None)))
    pkgs = pkgs_result.scalars().all()
    existing_ids = {p.id for p in cat.packages}
    for pkg in pkgs:
        if pkg.id not in existing_ids:
            cat.packages.append(pkg)
    await db.commit()
    await clear_cache_prefix_async("packages:")
    return {"assigned": len(pkgs)}

@category_router.delete("/{category_id}/packages/{package_id}", status_code=200)
async def remove_package_from_category(
    category_id: int,
    package_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Remove a specific package from a category."""
    result = await db.execute(select(PackageCategory).where(PackageCategory.id == category_id, PackageCategory.deleted_at.is_(None)).options(selectinload(PackageCategory.packages)))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    cat.packages = [p for p in cat.packages if p.id != package_id]
    await db.commit()
    await clear_cache_prefix_async("packages:")
    return {"removed": package_id}

router.include_router(category_router)


@router.get("/{package_id}", response_model=PackageDetailResponse)
async def get_package(
    package_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get detailed information for a single package with all content sections eagerly loaded."""
    query = (
        select(Package)
        .where(Package.id == package_id, Package.deleted_at.is_(None))
        .options(*full_package_options())
    )
    result = await db.execute(query)
    package = result.scalar_one_or_none()
    
    if not package:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Package not found"
        )
        
    return package

@router.post("", response_model=PackageDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_package(
    body: PackageCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Create a new package with all nested relations in one transaction."""
    # Ensure slug is unique
    slug = slugify(body.slug) if body.slug else slugify(body.title)
    
    existing = await db.execute(select(Package).where(Package.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{int(func.now().select().scalar_one().timestamp())}"
        
    package_data = body.model_dump(exclude={
        "variants", "transport_options", "gallery", "itinerary", "highlights", "inclusions", 
        "exclusions", "boarding_points", "faqs", "policies", "meals", "extras"
    })
    package_data["slug"] = slug
    
    package = Package(**package_data)
    
    # Sync child relations before saving
    await sync_nested_relation(db, package, "variants", PackageVariant, body.variants)
    await sync_nested_relation(db, package, "transport_options", PackageTransportOption, body.transport_options)
    await sync_nested_relation(db, package, "gallery", PackageGalleryImage, body.gallery)
    await sync_nested_relation(db, package, "itinerary", PackageItineraryDay, body.itinerary)
    await sync_nested_relation(db, package, "highlights", PackageHighlight, body.highlights)
    await sync_nested_relation(db, package, "inclusions", PackageInclusion, body.inclusions)
    await sync_nested_relation(db, package, "exclusions", PackageExclusion, body.exclusions)
    await sync_nested_relation(db, package, "boarding_points", PackageBoardingPoint, body.boarding_points)
    await sync_nested_relation(db, package, "faqs", PackageFAQ, body.faqs)
    await sync_nested_relation(db, package, "policies", PackagePolicy, body.policies)
    await sync_nested_relation(db, package, "meals", PackageMealItem, body.meals)
    await sync_nested_relation(db, package, "extras", PackageExtra, body.extras)
    
    # Compute starting_price — use student_price for student packages
    if package.is_student_package:
        package.starting_price = min(
            (v.student_price for v in package.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'student_price', None) and v.student_price > 0),
            default=0
        )
    else:
        package.starting_price = min(
            (v.adult_price for v in package.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'adult_price', 0) > 0),
            default=0
        )
    
    db.add(package)
    await db.commit()
    
    # Reload package with all options
    query = select(Package).where(Package.id == package.id).options(*full_package_options())
    result = await db.execute(query)
    package = result.scalar_one()
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="CREATE_PACKAGE",
        entity_type="Package",
        entity_id=str(package.id),
        details={"title": package.title, "slug": package.slug}
    )
    await db.commit()
    clear_cache_prefix("packages:")
    clear_cache_prefix("carousel:")
    
    return package

@router.put("/{package_id}", response_model=PackageDetailResponse)
async def update_package(
    package_id: int,
    body: PackageUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Update an existing package and all nested relations in one transaction."""
    query = (
        select(Package)
        .where(Package.id == package_id, Package.deleted_at.is_(None))
        .options(*full_package_options())
    )
    result = await db.execute(query)
    package = result.scalar_one_or_none()
    
    if not package:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Package not found"
        )
        
    update_data = body.model_dump(exclude_unset=True, exclude={
        "variants", "transport_options", "gallery", "itinerary", "highlights", "inclusions", 
        "exclusions", "boarding_points", "faqs", "policies", "meals", "extras"
    })

    # If slug is changing, verify uniqueness
    if "slug" in update_data and update_data["slug"]:
        update_data["slug"] = slugify(update_data["slug"])
        if update_data["slug"] != package.slug:
            existing = await db.execute(select(Package).where(Package.slug == update_data["slug"]))
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Slug already in use"
                )
            
    old_slug = package.slug
    old_brochure_pdf_url = package.brochure_pdf_url
    old_generated_brochure_url = package.generated_brochure_url

    for key, value in update_data.items():
        setattr(package, key, value)

    if "brochure_pdf_url" in update_data and update_data["brochure_pdf_url"] != old_brochure_pdf_url:
        from app.models.enums import DocumentGenerationStatus
        from app.services.pdf_generator import sync_cloudinary_delete
        import asyncio

        if old_generated_brochure_url:
            await asyncio.to_thread(sync_cloudinary_delete, old_generated_brochure_url)
        package.generated_brochure_url = None
        package.brochure_generation_status = (
            DocumentGenerationStatus.AVAILABLE
            if update_data["brochure_pdf_url"]
            else DocumentGenerationStatus.MISSING
        )
        
    # Sync child relations if provided in update payload
    if body.variants is not None:
        await sync_nested_relation(db, package, "variants", PackageVariant, body.variants)
    if getattr(body, "transport_options", None) is not None:
        await sync_nested_relation(db, package, "transport_options", PackageTransportOption, body.transport_options)
    if body.gallery is not None:
        await sync_nested_relation(db, package, "gallery", PackageGalleryImage, body.gallery)
    if body.itinerary is not None:
        await sync_nested_relation(db, package, "itinerary", PackageItineraryDay, body.itinerary)
    if body.highlights is not None:
        await sync_nested_relation(db, package, "highlights", PackageHighlight, body.highlights)
    if body.inclusions is not None:
        await sync_nested_relation(db, package, "inclusions", PackageInclusion, body.inclusions)
    if body.exclusions is not None:
        await sync_nested_relation(db, package, "exclusions", PackageExclusion, body.exclusions)
    if body.boarding_points is not None:
        await sync_nested_relation(db, package, "boarding_points", PackageBoardingPoint, body.boarding_points)
    if body.faqs is not None:
        await sync_nested_relation(db, package, "faqs", PackageFAQ, body.faqs)
    if body.policies is not None:
        await sync_nested_relation(db, package, "policies", PackagePolicy, body.policies)
    if body.meals is not None:
        await sync_nested_relation(db, package, "meals", PackageMealItem, body.meals)
    if body.extras is not None:
        await sync_nested_relation(db, package, "extras", PackageExtra, body.extras)
        
    # Recompute starting_price — use student_price for student packages
    if package.is_student_package:
        package.starting_price = min(
            (v.student_price for v in package.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'student_price', None) and v.student_price > 0),
            default=0
        )
    else:
        package.starting_price = min(
            (v.adult_price for v in package.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'adult_price', 0) > 0),
            default=0
        )
        
    await db.commit()
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="UPDATE_PACKAGE",
        entity_type="Package",
        entity_id=str(package.id),
        details=body.model_dump(exclude_unset=True, exclude={
            "variants", "transport_options", "gallery", "itinerary", "highlights", "inclusions", 
            "exclusions", "boarding_points", "faqs", "policies"
        })
    )
    await db.commit()
    
    # Broadcast SSE for Admin Package Edit
    import time
    from app.core.timezone import get_ist_now
    from app.utils.sse import sse_manager
    sse_payload = {
        "version": int(time.time() * 1000),
        "timestamp": get_ist_now().isoformat(),
        "package_id": package.id,
        "status": package.status
    }
    await sse_manager.broadcast_event("package", str(package.id), "ENTITY_STATUS_UPDATE", sse_payload)
        
    clear_cache_prefix("packages:list:")
    clear_cache_prefix(f"packages:detail:{old_slug}")
    clear_cache_prefix(f"packages:detail:{package.slug}")
    clear_cache_prefix("carousel:")
    from app.utils.cache import trigger_frontend_revalidation
    trigger_frontend_revalidation(tags=["packages", f"package:{package.slug}", f"package:{old_slug}"])
    
    # Reload package with all options to refresh expired attributes for Pydantic serialization
    refresh_query = select(Package).where(Package.id == package.id).options(*full_package_options())
    refresh_result = await db.execute(refresh_query)
    package = refresh_result.scalar_one()
    
    return package

@router.delete("/{package_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_package(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Delete a package with audit logging."""
    result = await db.execute(select(Package).where(Package.id == package_id))
    package = result.scalar_one_or_none()
    
    if not package:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Package not found"
        )
        
    package.deleted_at = func.now()
    await db.commit()
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="DELETE_PACKAGE",
        entity_type="Package",
        entity_id=str(package.id),
        details={"title": package.title}
    )
    await db.commit()
    
    # Broadcast SSE for Admin Package Delete
    import time
    from app.core.timezone import get_ist_now
    from app.utils.sse import sse_manager
    sse_payload = {
        "version": int(time.time() * 1000),
        "timestamp": get_ist_now().isoformat(),
        "package_id": package_id,
        "status": "DELETED"
    }
    await sse_manager.broadcast_event("package", str(package_id), "ENTITY_STATUS_UPDATE", sse_payload)

    clear_cache_prefix("packages:list:")
    clear_cache_prefix(f"packages:detail:{package.slug}")
    from app.utils.cache import trigger_frontend_revalidation
    trigger_frontend_revalidation(tags=["packages", f"package:{package.slug}"])
    
    return None

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks

@router.post("/{package_id}/publish", response_model=PackageDetailResponse)
async def publish_package(
    package_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """
    Publish Validation Engine.
    
    Transitions a package from DRAFT to PUBLISHED only if ALL operational
    requirements are met. Returns 400 with specific validation errors otherwise.
    
    Hard backend validation — no frontend-only bypass is possible.
    """
    query = (
        select(Package)
        .where(Package.id == package_id, Package.deleted_at.is_(None))
        .options(
            selectinload(Package.variants),
            selectinload(Package.transport_options),
            selectinload(Package.gallery),
            selectinload(Package.itinerary),
            selectinload(Package.highlights),
            selectinload(Package.inclusions),
            selectinload(Package.exclusions),
            selectinload(Package.boarding_points),
            selectinload(Package.faqs),
            selectinload(Package.policies)
        )
    )
    result = await db.execute(query)
    package = result.scalar_one_or_none()
    
    if not package:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Package not found"
        )
    
    # Collect all validation failures
    errors = []
    
    # Rule 1: At least one gallery image
    active_gallery = [g for g in package.gallery if g.deleted_at is None]
    if len(active_gallery) == 0:
        errors.append("At least one gallery image is required.")
    
    # Rule 2: At least one boarding/reporting point with map data
    active_boarding = [b for b in package.boarding_points if b.deleted_at is None]
    if len(active_boarding) == 0:
        errors.append("At least one reporting/boarding point is required.")
    else:
        has_map = any(b.map_url or b.address for b in active_boarding)
        if not has_map:
            errors.append("At least one boarding point must have an address or map link.")
    
    # Rule 3: At least one itinerary/journey stop
    active_itinerary = [i for i in package.itinerary if i.deleted_at is None]
    if len(active_itinerary) == 0:
        errors.append("At least one journey stop / itinerary day is required.")
    
    # Rule 4: At least one active variant
    active_variants = [v for v in package.variants if v.is_active and v.deleted_at is None]
    if len(active_variants) == 0:
        errors.append("At least one active transport/fare variant is required.")
    
    # Rule 4b: At least one active transport option when has_transport is True
    if package.has_transport:
        active_transport = [t for t in package.transport_options if t.deleted_at is None]
        if len(active_transport) == 0:
            errors.append("At least one transport option is required when 'Has Transport Options' is enabled.")
    
    # Rule 5: At least one policy
    active_policies = [p for p in package.policies if p.deleted_at is None]
    if len(active_policies) == 0:
        errors.append("At least one travel policy is required.")
    
    # Rule 6: At least one FAQ
    active_faqs = [f for f in package.faqs if f.deleted_at is None]
    if len(active_faqs) == 0:
        errors.append("At least one FAQ is required.")
    
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Package cannot be published. The following requirements are not met:",
                "validation_errors": errors
            }
        )
    
    # All checks passed — publish
    from app.models.enums import PublishStatus
    package.status = PublishStatus.PUBLISHED
    package.is_active = True
    
    await db.commit()
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="PUBLISH_PACKAGE",
        entity_type="Package",
        entity_id=str(package.id),
        details={"title": package.title, "status": "PUBLISHED"}
    )
    await db.commit()
    clear_cache_prefix("packages:list:")
    clear_cache_prefix(f"packages:detail:{package.slug}")
    from app.utils.cache import trigger_frontend_revalidation
    trigger_frontend_revalidation(tags=["packages", f"package:{package.slug}"])
    
    # ─── Document Architecture Trigger ─────────────────────────────
    from app.models.enums import DocumentGenerationStatus
    package.brochure_generation_status = DocumentGenerationStatus.QUEUED
    await db.commit()

    from app.services.pdf_generator import generate_package_brochure_task
    from app.core.config import settings
    import logging
    logger = logging.getLogger(__name__)

    if settings.ENVIRONMENT == "development":
        logger.info(f"Local development mode: triggering brochure generation task for package {package.id} inline via FastAPI BackgroundTasks.")
        background_tasks.add_task(generate_package_brochure_task, None, package.id)
    else:
        try:
            from app.worker import get_arq_pool
            import uuid
            arq_pool = await get_arq_pool()
            await arq_pool.enqueue_job(
                "generate_package_brochure_task", 
                package.id, 
                _job_id=f"brochure_pkg_{package.id}_{uuid.uuid4().hex[:8]}"
            )
        except Exception as e:
            logger.warning(f"ARQ enqueuing failed, falling back to inline FastAPI BackgroundTasks: {e}")
            background_tasks.add_task(generate_package_brochure_task, None, package.id)
    
    # Re-query the package with all options to prevent expired attributes during Pydantic serialization
    refresh_query = select(Package).where(Package.id == package.id).options(*full_package_options())
    refresh_result = await db.execute(refresh_query)
    return refresh_result.scalar_one()

@router.get("/{package_id}/brochure-validation")
async def get_brochure_validation(
    package_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Validate if the package meets all pre-flight conditions for brochure generation.
    """
    query = (
        select(Package)
        .where(Package.id == package_id, Package.deleted_at.is_(None))
        .options(
            selectinload(Package.variants),
            selectinload(Package.transport_options),
            selectinload(Package.gallery),
            selectinload(Package.itinerary),
            selectinload(Package.inclusions),
            selectinload(Package.exclusions),
            selectinload(Package.boarding_points),
            selectinload(Package.policies)
        )
    )
    result = await db.execute(query)
    package = result.scalar_one_or_none()
    
    if not package:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
        
    errors = []
    warnings = []
    
    if not package.title or not package.title.strip():
        errors.append("Package title is empty.")
    if not package.cover_image_url or not package.cover_image_url.strip():
        errors.append("Cover image is missing.")
    if not package.variants:
        errors.append("At least one price variant is required.")
    if not package.itinerary:
        errors.append("At least one day in itinerary is required.")
    if not package.inclusions:
        errors.append("At least one inclusion item is required.")
    if not package.exclusions:
        errors.append("At least one exclusion item is required.")
    if not package.boarding_points:
        errors.append("At least one boarding point is required.")
    if not package.policies:
        errors.append("At least one cancellation or general policy is required.")
        
    if package.gallery and len(package.gallery) < 1:
        warnings.append(f"Standard layout expects at least 1 gallery image. Currently has {len(package.gallery)}.")
        
    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "package_title": package.title,
        "status": package.brochure_generation_status,
        "generated_brochure_url": package.generated_brochure_url,
        "brochure_pdf_url": package.brochure_pdf_url,
        "active_brochure_url": package.brochure_pdf_url or package.generated_brochure_url
    }

@router.post("/{package_id}/regenerate-brochure", status_code=status.HTTP_202_ACCEPTED)
async def regenerate_brochure(
    package_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Manually trigger PDF regeneration for a package."""
    query = (
        select(Package)
        .where(Package.id == package_id, Package.deleted_at.is_(None))
        .options(
            selectinload(Package.variants),
            selectinload(Package.transport_options),
            selectinload(Package.gallery),
            selectinload(Package.itinerary),
            selectinload(Package.inclusions),
            selectinload(Package.exclusions),
            selectinload(Package.boarding_points),
            selectinload(Package.policies)
        )
    )
    result = await db.execute(query)
    package = result.scalar_one_or_none()
    
    if not package:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
        
    errors = []
    if not package.title or not package.title.strip():
        errors.append("Package title is empty.")
    if not package.cover_image_url or not package.cover_image_url.strip():
        errors.append("Cover image is missing.")
    if not package.variants:
        errors.append("At least one price variant is required.")
    if not package.itinerary:
        errors.append("At least one day in itinerary is required.")
    if not package.inclusions:
        errors.append("At least one inclusion item is required.")
    if not package.exclusions:
        errors.append("At least one exclusion item is required.")
    if not package.boarding_points:
        errors.append("At least one boarding point is required.")
    if not package.policies:
        errors.append("At least one policy is required.")
        
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Cannot generate brochure. Mandatory fields are missing.",
                "errors": errors
            }
        )
        
    from app.models.enums import DocumentGenerationStatus
    package.brochure_generation_status = DocumentGenerationStatus.QUEUED
    await db.commit()
    
    from app.services.pdf_generator import generate_package_brochure_task
    from app.core.config import settings
    import logging
    logger = logging.getLogger(__name__)

    if settings.ENVIRONMENT == "development":
        logger.info(f"Local development: triggering brochure regeneration for package {package.id} inline via FastAPI BackgroundTasks.")
        background_tasks.add_task(generate_package_brochure_task, None, package.id)
    else:
        try:
            from app.worker import get_arq_pool
            import uuid
            arq_pool = await get_arq_pool()
            await arq_pool.enqueue_job(
                "generate_package_brochure_task", 
                package.id, 
                _job_id=f"brochure_pkg_{package.id}_{uuid.uuid4().hex[:8]}"
            )
        except Exception as e:
            logger.warning(f"ARQ enqueuing failed, falling back to inline FastAPI BackgroundTasks: {e}")
            background_tasks.add_task(generate_package_brochure_task, None, package.id)
    
    await log_action(db, current_admin.id, "REGENERATE_BROCHURE", "Package", str(package.id), {"title": package.title})
    await db.commit()
    clear_cache_prefix(f"packages:detail:{package.slug}")
    
    return {"message": "Brochure regeneration started."}

@router.get("/{package_id}/future-bookings")
async def get_future_bookings(
    package_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Retrieve all future active bookings for this package."""
    # First, get all variant IDs for this package
    variant_query = select(PackageVariant.id).where(PackageVariant.package_id == package_id)
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
            Booking.variant_id.in_(variant_ids),
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


# ── Admin Package Category CRUD ───────────────────────────────────────────────

