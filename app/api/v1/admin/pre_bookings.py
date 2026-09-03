"""
Admin Pre-Bookings API — manage pre-booking leads.
"""
from datetime import date, datetime
from typing import Optional
import urllib.parse
from fastapi import APIRouter, Depends, Query, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, desc

from app.db.session import get_db
from app.middleware.auth import require_admin
from app.models.pre_booking import PreBooking
from app.models.user import User

router = APIRouter(
    prefix="/pre-bookings",
    tags=["Admin - Pre-Bookings"],
)


class PreBookingAdminOut(BaseModel):
    id: int
    ref_id: str
    package_id: str
    package_name: str
    travel_date: date
    adult_count: int
    child_count: int
    customer_name: str
    customer_email: str
    customer_phone: str
    notes: Optional[str]
    is_confirmed: bool
    is_contacted: bool
    admin_notes: Optional[str]
    user_email_sent: bool
    admin_email_sent: bool
    created_at: datetime
    whatsapp_url: str

    class Config:
        from_attributes = True


def _build_wa_url(pb: PreBooking) -> str:
    phone = pb.customer_phone.replace("+91", "").replace(" ", "").replace("-", "")
    if not phone.startswith("91"):
        phone = "91" + phone
    travel_str = pb.travel_date.strftime("%d %B %Y") if pb.travel_date else "—"
    pax = f"{pb.adult_count} adult{'s' if pb.adult_count != 1 else ''}"
    if pb.child_count:
        pax += f" + {pb.child_count} child{'ren' if pb.child_count > 1 else ''}"
    msg = (
        f"Hello {pb.customer_name}! This is TS Boat Tourism. "
        f"We received your pre-booking for {pb.package_name} on {travel_str} ({pax}). "
        f"Your PNR Number is {pb.ref_id}. We would love to confirm your slot — are you available?"
    )
    return f"https://wa.me/{phone}?text={urllib.parse.quote(msg)}"


def _serialize(pb: PreBooking) -> dict:
    return {
        "id": pb.id,
        "ref_id": pb.ref_id,
        "package_id": pb.package_id,
        "package_name": pb.package_name,
        "travel_date": pb.travel_date,
        "adult_count": pb.adult_count,
        "child_count": pb.child_count,
        "customer_name": pb.customer_name,
        "customer_email": pb.customer_email,
        "customer_phone": pb.customer_phone,
        "notes": pb.notes,
        "is_confirmed": pb.is_confirmed,
        "is_contacted": pb.is_contacted,
        "admin_notes": pb.admin_notes,
        "user_email_sent": pb.user_email_sent,
        "admin_email_sent": pb.admin_email_sent,
        "created_at": pb.created_at,
        "whatsapp_url": _build_wa_url(pb),
    }


@router.get("")
async def list_pre_bookings(
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
    search: Optional[str] = Query(None),
    is_confirmed: Optional[bool] = Query(None),
    is_contacted: Optional[bool] = Query(None),
    package_id: Optional[str] = Query(None),
    travel_date: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List all pre-booking leads with filters and pagination."""
    q = select(PreBooking).where(PreBooking.deleted_at.is_(None))

    if search and search.strip():
        like = f"%{search.strip()}%"
        q = q.where(or_(
            PreBooking.customer_name.ilike(like),
            PreBooking.customer_email.ilike(like),
            PreBooking.customer_phone.ilike(like),
            PreBooking.ref_id.ilike(like),
            PreBooking.package_name.ilike(like),
        ))
    if is_confirmed is not None:
        q = q.where(PreBooking.is_confirmed == is_confirmed)
    if is_contacted is not None:
        q = q.where(PreBooking.is_contacted == is_contacted)
    if package_id and package_id.strip():
        q = q.where(PreBooking.package_id == package_id.strip())
    if travel_date:
        q = q.where(PreBooking.travel_date == travel_date)

    count_q = select(func.count()).select_from(q.subquery())
    total_res = await db.execute(count_q)
    total = total_res.scalar() or 0

    q = q.order_by(desc(PreBooking.created_at)).limit(limit).offset(offset)
    result = await db.execute(q)
    items = result.scalars().all()

    return {
        "total": total,
        "items": [_serialize(pb) for pb in items],
        "limit": limit,
        "offset": offset,
    }


@router.get("/stats")
async def get_pre_booking_stats(
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Quick stats for the admin dashboard widget."""
    base = select(func.count(PreBooking.id)).where(PreBooking.deleted_at.is_(None))
    total_res = await db.execute(base)
    total = total_res.scalar() or 0

    pending_res = await db.execute(
        select(func.count(PreBooking.id)).where(
            PreBooking.deleted_at.is_(None),
            PreBooking.is_confirmed == False,  # noqa: E712
        )
    )
    pending = pending_res.scalar() or 0

    not_contacted_res = await db.execute(
        select(func.count(PreBooking.id)).where(
            PreBooking.deleted_at.is_(None),
            PreBooking.is_contacted == False,  # noqa: E712
        )
    )
    not_contacted = not_contacted_res.scalar() or 0

    confirmed_res = await db.execute(
        select(func.count(PreBooking.id)).where(
            PreBooking.deleted_at.is_(None),
            PreBooking.is_confirmed == True,  # noqa: E712
        )
    )
    confirmed = confirmed_res.scalar() or 0

    return {
        "total": total,
        "pending": pending,
        "confirmed": confirmed,
        "not_contacted": not_contacted,
    }


@router.get("/{pb_id}")
async def get_pre_booking(
    pb_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Get a single pre-booking by ID."""
    result = await db.execute(
        select(PreBooking).where(PreBooking.id == pb_id, PreBooking.deleted_at.is_(None))
    )
    pb = result.scalar_one_or_none()
    if not pb:
        raise HTTPException(status_code=404, detail="Pre-booking not found.")
    return _serialize(pb)


class UpdatePreBooking(BaseModel):
    is_confirmed: Optional[bool] = None
    is_contacted: Optional[bool] = None
    admin_notes: Optional[str] = None


@router.patch("/{pb_id}")
async def update_pre_booking(
    pb_id: int,
    body: UpdatePreBooking,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Update confirmation/contact status and admin notes."""
    result = await db.execute(
        select(PreBooking).where(PreBooking.id == pb_id, PreBooking.deleted_at.is_(None))
    )
    pb = result.scalar_one_or_none()
    if not pb:
        raise HTTPException(status_code=404, detail="Pre-booking not found.")

    if body.is_confirmed is not None:
        pb.is_confirmed = body.is_confirmed
    if body.is_contacted is not None:
        pb.is_contacted = body.is_contacted
    if body.admin_notes is not None:
        pb.admin_notes = body.admin_notes

    await db.commit()
    await db.refresh(pb)
    return _serialize(pb)


@router.delete("/{pb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pre_booking(
    pb_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Soft delete a pre-booking lead."""
    from app.core.timezone import get_ist_now
    result = await db.execute(
        select(PreBooking).where(PreBooking.id == pb_id, PreBooking.deleted_at.is_(None))
    )
    pb = result.scalar_one_or_none()
    if not pb:
        raise HTTPException(status_code=404, detail="Pre-booking not found.")

    pb.deleted_at = get_ist_now()
    await db.commit()
    return None
