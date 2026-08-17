from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.session import get_db
from app.models.user import User
from app.models.booking import Booking
from app.models.package import Package, PackageCategory
from app.models.room import Room, RoomCategory, RoomVariant
from app.middleware.auth import require_admin

router = APIRouter(
    prefix="/dashboard",
    tags=["Admin - Dashboard"],
    dependencies=[Depends(require_admin)]
)

@router.get("/stats")
async def get_dashboard_stats(db: AsyncSession = Depends(get_db)):
    """
    Returns high-level KPIs for the admin dashboard.
    Requires ADMIN role.
    """
    result = await db.execute(
        select(
            select(func.count()).select_from(Package).where(Package.deleted_at.is_(None)).scalar_subquery(),
            select(func.count()).select_from(PackageCategory).where(PackageCategory.deleted_at.is_(None)).scalar_subquery(),
            select(func.count()).select_from(RoomCategory).where(RoomCategory.deleted_at.is_(None)).scalar_subquery(),
            select(func.count()).select_from(RoomVariant).where(RoomVariant.deleted_at.is_(None)).scalar_subquery(),
            select(func.count()).select_from(Booking).where(Booking.deleted_at.is_(None)).scalar_subquery(),
            select(func.count()).select_from(User).where(User.deleted_at.is_(None)).scalar_subquery(),
        )
    )
    packages_count, package_categories_count, room_categories_count, room_variants_count, bookings_count, users_count = result.one()

    # Get recent bookings
    from app.models.package import PackageVariant
    
    recent_result = await db.execute(
        select(Booking)
        .where(Booking.deleted_at.is_(None))
        .order_by(Booking.created_at.desc())
        .limit(5)
    )
    raw_recent = recent_result.scalars().all()

    # Collect variant IDs for batch lookup
    variant_ids = [b.variant_id for b in raw_recent if b.variant_id]
    room_variant_ids = [b.room_variant_id for b in raw_recent if b.room_variant_id]

    pkg_titles = {}
    if variant_ids:
        pkg_res = await db.execute(
            select(PackageVariant.id, Package.title)
            .join(Package, PackageVariant.package_id == Package.id)
            .where(PackageVariant.id.in_(variant_ids))
        )
        pkg_titles = {v_id: title for v_id, title in pkg_res.all()}

    room_titles = {}
    if room_variant_ids:
        rm_res = await db.execute(
            select(RoomVariant.id, Room.lodge_name)
            .join(Room, RoomVariant.room_id == Room.id)
            .where(RoomVariant.id.in_(room_variant_ids))
        )
        room_titles = {rv_id: name for rv_id, name in rm_res.all()}

    recent_bookings = []
    for b in raw_recent:
        booking_title = None
        if b.variant_id:
            booking_title = pkg_titles.get(b.variant_id)
        elif b.room_variant_id:
            booking_title = room_titles.get(b.room_variant_id)

        if not booking_title:
            booking_title = "Package Booking" if b.variant_id else ("Room Booking" if b.room_variant_id else "Booking")

        recent_bookings.append({
            "id": b.id,
            "public_id": b.public_id,
            "title": booking_title,
            "amount": float(b.total_amount),
            "status": b.status.value if hasattr(b.status, "value") else str(b.status),
            "created_at": b.created_at.isoformat() if b.created_at else None
        })

    # Get status counts
    status_result = await db.execute(
        select(Booking.status, func.count(Booking.id))
        .where(Booking.deleted_at.is_(None))
        .group_by(Booking.status)
    )
    
    analysis = {"CONFIRMED": 0, "PENDING": 0, "PARTIAL_PAID": 0, "CANCELLED": 0, "REFUNDED": 0}
    for status, count in status_result.all():
        s = status.value if hasattr(status, "value") else str(status)
        if s == "FULLY_PAID": s = "CONFIRMED"
        analysis[s] = count

    # Calculate revenue
    revenue_result = await db.execute(
        select(func.sum(Booking.total_amount))
        .where(Booking.deleted_at.is_(None), Booking.status == "FULLY_PAID")
    )
    total_revenue = float(revenue_result.scalar() or 0.00)

    return {
        "packages": packages_count,
        "package_categories": package_categories_count,
        "room_categories": room_categories_count,
        "room_types": room_variants_count,
        "bookings": bookings_count,
        "users": users_count,
        "total_revenue": total_revenue,
        "recent_bookings": recent_bookings,
        "analysis": analysis
    }
