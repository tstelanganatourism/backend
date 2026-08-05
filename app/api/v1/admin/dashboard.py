from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.session import get_db
from app.models.user import User
from app.models.booking import Booking
from app.models.package import Package
from app.models.room import Room
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
            select(func.count()).select_from(Room).where(Room.deleted_at.is_(None)).scalar_subquery(),
            select(func.count()).select_from(Booking).where(Booking.deleted_at.is_(None)).scalar_subquery(),
            select(func.count()).select_from(User).where(User.deleted_at.is_(None)).scalar_subquery(),
        )
    )
    packages_count, rooms_count, bookings_count, users_count = result.one()

    # Get recent bookings
    from app.models.package import Package, PackageVariant
    from app.models.room import Room, RoomVariant
    from sqlalchemy.orm import selectinload
    
    recent_result = await db.execute(
        select(Booking)
        .where(Booking.deleted_at.is_(None))
        .order_by(Booking.created_at.desc())
        .limit(5)
    )
    recent_bookings = []
    
    for b in recent_result.scalars().all():
        booking_title = None
        if b.variant_id:
            pkg_res = await db.execute(
                select(Package.title)
                .join(PackageVariant, PackageVariant.package_id == Package.id)
                .where(PackageVariant.id == b.variant_id)
            )
            booking_title = pkg_res.scalar_one_or_none()
        elif b.room_variant_id:
            rm_res = await db.execute(
                select(Room.lodge_name)
                .join(RoomVariant, RoomVariant.room_id == Room.id)
                .where(RoomVariant.id == b.room_variant_id)
            )
            booking_title = rm_res.scalar_one_or_none()
            
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
        "rooms": rooms_count,
        "bookings": bookings_count,
        "users": users_count,
        "total_revenue": total_revenue,
        "recent_bookings": recent_bookings,
        "analysis": analysis
    }
