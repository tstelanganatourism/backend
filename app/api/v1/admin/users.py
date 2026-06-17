"""
Admin User Management Router — List, Detail (with bookings), Password Reset, and soft-delete for tourist users.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_, asc, desc
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel, Field

from app.db.session import get_db
from app.models.user import User
from app.models.booking import Booking
from app.models.enums import UserRole, AccountStatus
from app.middleware.auth import require_admin
from app.core.security import get_password_hash
from app.core.timezone import get_ist_now
from app.utils.audit import log_action

router = APIRouter(
    prefix="/users",
    tags=["Admin - User Management"],
    dependencies=[Depends(require_admin)],
)

# ─── Schemas ──────────────────────────────────────────────────────────────────

class AdminUserResponse(BaseModel):
    id: int
    full_name: str
    email: Optional[str] = None
    phone_number: Optional[str] = None
    role: str
    account_status: str
    is_active: bool
    avatar_url: Optional[str] = None
    created_at: Optional[datetime] = None
    total_bookings: int = 0

class AdminUserPaginatedResponse(BaseModel):
    items: List[AdminUserResponse]
    total: int
    page: int
    size: int

class AdminUserBookingItem(BaseModel):
    id: int
    public_id: str
    target_type: str
    status: str
    travel_date: str
    total_amount: float
    paid_amount: float
    remaining_balance: float
    created_at: Optional[datetime] = None
    package_title: str
    variant_title: str

class AdminUserDetailResponse(BaseModel):
    id: int
    full_name: str
    email: Optional[str] = None
    phone_number: Optional[str] = None
    role: str
    account_status: str
    is_active: bool
    avatar_url: Optional[str] = None
    created_at: Optional[datetime] = None
    bookings: List[AdminUserBookingItem] = []

class AdminUserPasswordReset(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)

# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("", response_model=AdminUserPaginatedResponse)
async def list_users(
    search: Optional[str] = Query(None, description="Search by name, email, or phone"),
    status_filter: Optional[str] = Query(None, description="ACTIVE, BLOCKED, DISABLED"),
    sort_by: Optional[str] = Query("created_at", description="Sort field"),
    sort_order: Optional[str] = Query("desc", description="asc or desc"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List all tourist users with optional search, filtering, and sorting."""
    # Count Query
    count_query = select(func.count(User.id)).where(
        User.role == UserRole.USER,
        User.deleted_at.is_(None)
    )

    query = select(
        User, 
        func.count(Booking.id).label("total_bookings")
    ).outerjoin(
        Booking,
        (Booking.user_id == User.id) & Booking.deleted_at.is_(None)
    ).where(
        User.role == UserRole.USER,
        User.deleted_at.is_(None),
    ).group_by(User.id)

    # Search filter
    if search:
        search_term = f"%{search}%"
        query = query.where(
            or_(
                User.full_name.ilike(search_term),
                User.email.ilike(search_term),
                User.phone_number.ilike(search_term),
            )
        )
        count_query = count_query.where(
            or_(
                User.full_name.ilike(search_term),
                User.email.ilike(search_term),
                User.phone_number.ilike(search_term),
            )
        )

    # Status filter
    if status_filter:
        status_upper = status_filter.upper()
        if status_upper in ("ACTIVE", "BLOCKED", "DISABLED"):
            target_status = AccountStatus(status_upper)
            query = query.where(User.account_status == target_status)
            count_query = count_query.where(User.account_status == target_status)

    # Execute count
    total_result = await db.execute(count_query)
    total_count = total_result.scalar_one()

    # Sorting
    sort_column = getattr(User, sort_by, User.created_at)
    if sort_order and sort_order.lower() == "asc":
        query = query.order_by(asc(sort_column))
    else:
        query = query.order_by(desc(sort_column))

    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    users = result.all()

    items = []
    for row in users:
        u = row.User
        items.append(
            AdminUserResponse(
                id=u.id,
                full_name=u.full_name,
                email=u.email,
                phone_number=u.phone_number,
                role=u.role.value if hasattr(u.role, 'value') else str(u.role),
                account_status=u.account_status.value if hasattr(u.account_status, 'value') else str(u.account_status),
                is_active=u.is_active,
                avatar_url=u.avatar_url,
                created_at=u.created_at,
                total_bookings=row.total_bookings or 0
            )
        )

    return {
        "items": items,
        "total": total_count,
        "page": (offset // limit) + 1 if limit > 0 else 1,
        "size": limit
    }

@router.get("/{user_id}", response_model=AdminUserDetailResponse)
async def get_user_detail(
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get tourist user details along with their bookings list."""
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.role == UserRole.USER,
            User.deleted_at.is_(None)
        )
    )
    user_obj = result.scalar_one_or_none()
    if not user_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found."
        )

    # Query bookings for this user
    bookings_res = await db.execute(
        select(Booking)
        .where(Booking.user_id == user_id, Booking.deleted_at.is_(None))
        .order_by(Booking.created_at.desc())
    )
    bookings = bookings_res.scalars().all()

    # Load titles and variant info
    variant_ids = {b.variant_id for b in bookings if b.variant_id}
    room_variant_ids = {b.room_variant_id for b in bookings if b.room_variant_id}

    variant_map = {}
    if variant_ids:
        from app.models.package import PackageVariant, Package
        pv_res = await db.execute(
            select(PackageVariant, Package.title)
            .join(Package, Package.id == PackageVariant.package_id)
            .where(PackageVariant.id.in_(variant_ids))
        )
        for pv, pkg_title in pv_res.all():
            variant_map[pv.id] = {
                "package_title": pkg_title,
                "variant_title": pv.title,
            }

    room_variant_map = {}
    if room_variant_ids:
        from app.models.room import RoomVariant, Room
        rv_res = await db.execute(
            select(RoomVariant, Room.lodge_name)
            .join(Room, Room.id == RoomVariant.room_id)
            .where(RoomVariant.id.in_(room_variant_ids))
        )
        for rv, room_name in rv_res.all():
            room_variant_map[rv.id] = {
                "package_title": room_name,
                "variant_title": rv.variant_name,
            }

    booking_items = []
    for b in bookings:
        target_type = "ROOM" if b.room_variant_id else "PACKAGE"
        if b.variant_id and b.variant_id in variant_map:
            title_info = variant_map[b.variant_id]
        elif b.room_variant_id and b.room_variant_id in room_variant_map:
            title_info = room_variant_map[b.room_variant_id]
        else:
            title_info = {"package_title": "—", "variant_title": "—"}

        booking_items.append(
            AdminUserBookingItem(
                id=b.id,
                public_id=b.public_id,
                target_type=target_type,
                status=b.status.value if hasattr(b.status, "value") else str(b.status),
                travel_date=b.travel_date.isoformat(),
                total_amount=float(b.total_amount),
                paid_amount=float(b.paid_amount),
                remaining_balance=float(b.remaining_balance),
                created_at=b.created_at,
                package_title=title_info["package_title"],
                variant_title=title_info["variant_title"]
            )
        )

    return AdminUserDetailResponse(
        id=user_obj.id,
        full_name=user_obj.full_name,
        email=user_obj.email,
        phone_number=user_obj.phone_number,
        role=user_obj.role.value if hasattr(user_obj.role, 'value') else str(user_obj.role),
        account_status=user_obj.account_status.value if hasattr(user_obj.account_status, 'value') else str(user_obj.account_status),
        is_active=user_obj.is_active,
        avatar_url=user_obj.avatar_url,
        created_at=user_obj.created_at,
        bookings=booking_items
    )

@router.patch("/{user_id}/password", status_code=status.HTTP_200_OK)
async def reset_user_password(
    user_id: int,
    body: AdminUserPasswordReset,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Admin resets tourist user's password."""
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.role == UserRole.USER,
            User.deleted_at.is_(None)
        )
    )
    user_obj = result.scalar_one_or_none()
    if not user_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found."
        )

    user_obj.password_hash = get_password_hash(body.new_password)
    await db.commit()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="ADMIN_RESET_USER_PASSWORD",
        entity_type="User",
        entity_id=str(user_obj.id),
        details={"full_name": user_obj.full_name},
    )
    await db.commit()

    return {"message": f"Password reset successfully for {user_obj.full_name}."}

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Soft delete tourist user."""
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.role == UserRole.USER,
            User.deleted_at.is_(None)
        )
    )
    user_obj = result.scalar_one_or_none()
    if not user_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found."
        )

    user_obj.deleted_at = get_ist_now()
    user_obj.is_active = False
    user_obj.google_id = None
    if user_obj.email:
        user_obj.email = f"{user_obj.email}.deleted.{int(get_ist_now().timestamp())}"
    if user_obj.phone_number:
        user_obj.phone_number = f"{user_obj.phone_number}.deleted.{int(get_ist_now().timestamp())}"
    await db.commit()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="ADMIN_DELETE_USER",
        entity_type="User",
        entity_id=str(user_obj.id),
        details={"full_name": user_obj.full_name, "email": user_obj.email},
    )
    await db.commit()

    return None

@router.post("/{user_id}/toggle-status", response_model=AdminUserResponse)
async def toggle_user_status(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Toggle user status between ACTIVE and BLOCKED."""
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.role == UserRole.USER,
            User.deleted_at.is_(None)
        )
    )
    user_obj = result.scalar_one_or_none()
    if not user_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found."
        )

    if user_obj.account_status == AccountStatus.BLOCKED:
        user_obj.account_status = AccountStatus.ACTIVE
        user_obj.is_active = True
        new_status = "ACTIVE"
    else:
        user_obj.account_status = AccountStatus.BLOCKED
        user_obj.is_active = False
        new_status = "BLOCKED"

    await db.commit()
    await db.refresh(user_obj)

    # Query total bookings for mapping response
    bookings_count_res = await db.execute(
        select(func.count(Booking.id))
        .where(Booking.user_id == user_id, Booking.deleted_at.is_(None))
    )
    total_bookings = bookings_count_res.scalar_one()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="ADMIN_TOGGLE_USER_STATUS",
        entity_type="User",
        entity_id=str(user_obj.id),
        details={"full_name": user_obj.full_name, "new_status": new_status},
    )
    await db.commit()

    return AdminUserResponse(
        id=user_obj.id,
        full_name=user_obj.full_name,
        email=user_obj.email,
        phone_number=user_obj.phone_number,
        role=user_obj.role.value if hasattr(user_obj.role, 'value') else str(user_obj.role),
        account_status=user_obj.account_status.value if hasattr(user_obj.account_status, 'value') else str(user_obj.account_status),
        is_active=user_obj.is_active,
        avatar_url=user_obj.avatar_url,
        created_at=user_obj.created_at,
        total_bookings=total_bookings
    )


class AdminUserUpdatePayload(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=100)
    email: Optional[str] = Field(None, max_length=150)
    phone_number: Optional[str] = Field(None, pattern=r"^[0-9]{10}$")


@router.patch("/{user_id}", response_model=AdminUserResponse)
async def update_user_profile(
    user_id: int,
    body: AdminUserUpdatePayload,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Admin updates tourist user's details (full name, email, phone number)."""
    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.role == UserRole.USER,
            User.deleted_at.is_(None)
        )
    )
    user_obj = result.scalar_one_or_none()
    if not user_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found."
        )

    if body.full_name is not None:
        user_obj.full_name = body.full_name.strip()
    
    if body.email is not None:
        email_clean = body.email.strip().lower()
        if email_clean:
            # Check unique email
            email_res = await db.execute(
                select(User).where(User.email == email_clean, User.id != user_id, User.deleted_at.is_(None))
            )
            if email_res.scalar_one_or_none():
                raise HTTPException(status_code=400, detail="Email is already used by another account.")
            user_obj.email = email_clean
        else:
            user_obj.email = None

    if body.phone_number is not None:
        phone_clean = body.phone_number.strip()
        if phone_clean:
            # Check unique phone
            phone_res = await db.execute(
                select(User).where(User.phone_number == phone_clean, User.id != user_id, User.deleted_at.is_(None))
            )
            if phone_res.scalar_one_or_none():
                raise HTTPException(status_code=400, detail="Phone number is already used by another account.")
            user_obj.phone_number = phone_clean
        else:
            user_obj.phone_number = None

    await db.commit()
    await db.refresh(user_obj)

    # Query total bookings for mapping response
    bookings_count_res = await db.execute(
        select(func.count(Booking.id))
        .where(Booking.user_id == user_id, Booking.deleted_at.is_(None))
    )
    total_bookings = bookings_count_res.scalar_one()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="ADMIN_UPDATE_USER_PROFILE",
        entity_type="User",
        entity_id=str(user_obj.id),
        details={"full_name": user_obj.full_name, "email": user_obj.email, "phone_number": user_obj.phone_number},
    )
    await db.commit()

    return AdminUserResponse(
        id=user_obj.id,
        full_name=user_obj.full_name,
        email=user_obj.email,
        phone_number=user_obj.phone_number,
        role=user_obj.role.value if hasattr(user_obj.role, 'value') else str(user_obj.role),
        account_status=user_obj.account_status.value if hasattr(user_obj.account_status, 'value') else str(user_obj.account_status),
        is_active=user_obj.is_active,
        avatar_url=user_obj.avatar_url,
        created_at=user_obj.created_at,
        total_bookings=total_bookings
    )
