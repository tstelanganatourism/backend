from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, text
from typing import List, Optional
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
    PackageTransportOption
)
from app.schemas.package import PackageCreate, PackageUpdate, PackageDetailResponse, PackageResponse, PackagePaginatedResponse
from app.middleware.auth import require_admin
from app.models.user import User
from app.utils.audit import log_action
from app.utils.cache import clear_cache_prefix
import re

from sqlalchemy.orm import selectinload

router = APIRouter(
    prefix="/packages",
    tags=["Admin - Package CMS"],
    dependencies=[Depends(require_admin)]
)

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
            .where(Booking.status != BookingStatus.CANCELLED)
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

@router.get("/{package_id}", response_model=PackageDetailResponse)
async def get_package(
    package_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get detailed information for a single package with all content sections eagerly loaded."""
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
        
    return package

@router.post("", response_model=PackageDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_package(
    body: PackageCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Create a new package with all nested relations in one transaction."""
    # Ensure slug is unique
    slug = body.slug or slugify(body.title)
    
    existing = await db.execute(select(Package).where(Package.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{int(func.now().select().scalar_one().timestamp())}"
        
    package_data = body.model_dump(exclude={
        "variants", "transport_options", "gallery", "itinerary", "highlights", "inclusions", 
        "exclusions", "boarding_points", "faqs", "policies"
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
    
    # Compute starting_price
    package.starting_price = min(
        (v.adult_price for v in package.variants if v.is_active and not getattr(v, 'deleted_at', None) and getattr(v, 'adult_price', 0) > 0),
        default=0
    )
    
    db.add(package)
    await db.commit()
    
    # Reload package with all child relations loaded
    query = (
        select(Package)
        .where(Package.id == package.id)
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
        
    update_data = body.model_dump(exclude_unset=True, exclude={
        "variants", "transport_options", "gallery", "itinerary", "highlights", "inclusions", 
        "exclusions", "boarding_points", "faqs", "policies"
    })
    
    # If slug is changing, verify uniqueness
    if "slug" in update_data and update_data["slug"] != package.slug:
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
        from app.services.r2_storage import r2_service

        if old_generated_brochure_url:
            await r2_service.delete_file(old_generated_brochure_url)
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
        
    # Recompute starting_price
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
    
    # Broadcast SSE for Admin Package Edit if status is INACTIVE
    if package.status == "INACTIVE":
        import time
        from app.core.timezone import get_ist_now
        from app.utils.sse import sse_manager
        sse_payload = {
            "version": int(time.time() * 1000),
            "timestamp": get_ist_now().isoformat(),
            "package_id": package.id,
            "status": "INACTIVE"
        }
        await sse_manager.broadcast_event("package", str(package.id), "ENTITY_STATUS_UPDATE", sse_payload)
        
    clear_cache_prefix("packages:list:")
    clear_cache_prefix(f"packages:detail:{old_slug}")
    clear_cache_prefix(f"packages:detail:{package.slug}")
    clear_cache_prefix("carousel:")
    from app.utils.cache import trigger_frontend_revalidation
    trigger_frontend_revalidation(tags=[f"package-{package.id}"])
    
    # Reload package with all relationships loaded to prevent MissingGreenlet errors during serialization
    refresh_query = (
        select(Package)
        .where(Package.id == package.id)
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
    trigger_frontend_revalidation(tags=[f"package-{package.id}"])
    
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
    trigger_frontend_revalidation(tags=[f"package-{package.id}"])
    
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
    
    # Re-query the package with eager loads to prevent expired attributes during Pydantic serialization
    refresh_result = await db.execute(query)
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
        "active_brochure_url": package.generated_brochure_url or package.brochure_pdf_url
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
