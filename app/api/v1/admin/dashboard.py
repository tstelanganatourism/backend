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
            select(func.count()).select_from(Package).scalar_subquery(),
            select(func.count()).select_from(Room).scalar_subquery(),
            select(func.count()).select_from(Booking).scalar_subquery(),
            select(func.count()).select_from(User).scalar_subquery(),
        )
    )
    packages_count, rooms_count, bookings_count, users_count = result.one()

    return {
        "packages": packages_count,
        "rooms": rooms_count,
        "bookings": bookings_count,
        "users": users_count,
        # TODO: Add revenue calculations when payment engine is built
        "total_revenue": 0.00
    }
