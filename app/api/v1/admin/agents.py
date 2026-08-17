"""
Admin Agent Management Router — Full CRUD + metrics.

Endpoints:
  GET    /agents              — List agents with search, filter, sort, pagination
  GET    /agents/{id}         — Single agent detail with booking metrics
  POST   /agents              — Create agent
  PUT    /agents/{id}         — Update agent
  DELETE /agents/{id}         — Soft-delete agent
  POST   /agents/{id}/reset-password — Admin resets agent password
  POST   /agents/{id}/toggle-status  — Toggle ACTIVE/BLOCKED
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, case, desc, asc
from typing import List, Optional

from app.db.session import get_db
from app.models.user import User, AgentPackageQuota
from app.models.booking import Booking
from app.models.package import Package
from app.models.enums import UserRole, AccountStatus, BookingStatus
from app.schemas.agent import (
    AgentCreate,
    AgentUpdate,
    AgentResponse,
    AgentDetailResponse,
    AgentBookingMetrics,
    AgentResetPassword,
    AgentPaginatedResponse,
    AgentQuotaResponse,
    AgentQuotaUpdate,
)
from app.middleware.auth import require_admin
from app.core.security import get_password_hash
from app.core.timezone import get_ist_now
from app.utils.audit import log_action

router = APIRouter(
    prefix="/agents",
    tags=["Admin - Agent Management"],
    dependencies=[Depends(require_admin)],
)


# ─── List Agents ─────────────────────────────────────────────────────────────

@router.get("", response_model=AgentPaginatedResponse)
async def list_agents(
    search: Optional[str] = Query(None, description="Search by name, email, or phone"),
    status_filter: Optional[str] = Query(None, description="ACTIVE, BLOCKED, DISABLED"),
    sort_by: Optional[str] = Query("created_at", description="Sort field"),
    sort_order: Optional[str] = Query("desc", description="asc or desc"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List all agents with optional search, filtering, and sorting."""
    # Count Query
    count_query = select(func.count(User.id)).where(
        User.role == UserRole.AGENT,
        User.deleted_at.is_(None)
    )

    query = select(
        User, 
        func.count(Booking.id).label("total_bookings")
    ).outerjoin(
        Booking,
        (Booking.agent_id == User.id) & Booking.deleted_at.is_(None)
    ).where(
        User.role == UserRole.AGENT,
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
                User.company_name.ilike(search_term),
            )
        )
        count_query = count_query.where(
            or_(
                User.full_name.ilike(search_term),
                User.email.ilike(search_term),
                User.phone_number.ilike(search_term),
                User.company_name.ilike(search_term),
            )
        )

    # Status filter
    if status_filter:
        status_upper = status_filter.upper()
        if status_upper == "ACTIVE":
            query = query.where(User.account_status == AccountStatus.ACTIVE)
            count_query = count_query.where(User.account_status == AccountStatus.ACTIVE)
        elif status_upper == "BLOCKED":
            query = query.where(User.account_status == AccountStatus.BLOCKED)
            count_query = count_query.where(User.account_status == AccountStatus.BLOCKED)
        elif status_upper == "DISABLED":
            query = query.where(User.account_status == AccountStatus.DISABLED)
            count_query = count_query.where(User.account_status == AccountStatus.DISABLED)

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
    agents = result.all()

    return {
        "items": [_to_response(row.User, row.total_bookings) for row in agents],
        "total": total_count,
        "page": (offset // limit) + 1 if limit > 0 else 1,
        "size": limit
    }


# ─── Get Single Agent (with metrics) ─────────────────────────────────────────

@router.get("/{agent_id}", response_model=AgentDetailResponse)
async def get_agent(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get single agent detail including booking/commission metrics."""
    agent = await _get_agent_or_404(db, agent_id)

    # Aggregate booking metrics from the bookings table
    metrics = await _compute_agent_metrics(db, agent_id)

    resp = _to_response(agent)
    return AgentDetailResponse(
        **resp.model_dump(),
        metrics=metrics,
    )


# ─── Create Agent ─────────────────────────────────────────────────────────────

@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Create a new agent. Enforces unique email and phone."""
    # Check unique email
    existing_email = await db.execute(
        select(User).where(User.email == body.email, User.deleted_at.is_(None))
    )
    if existing_email.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email already exists.",
        )

    # Check unique phone
    existing_phone = await db.execute(
        select(User).where(User.phone_number == body.phone_number, User.deleted_at.is_(None))
    )
    if existing_phone.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this phone number already exists.",
        )

    agent = User(
        full_name=body.full_name,
        email=body.email,
        phone_number=body.phone_number,
        password_hash=get_password_hash(body.password),
        role=UserRole.AGENT,
        account_status=AccountStatus.ACTIVE,
        is_active=True,
        commission_type=body.commission_type,
        commission_percentage=body.commission_percentage,
        commission_fixed_amount=body.commission_fixed_amount,
        company_name=body.company_name,
        gst_number=body.gst_number,
        address=body.address,
        admin_notes=body.admin_notes,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="CREATE_AGENT",
        entity_type="User",
        entity_id=str(agent.id),
        details={
            "full_name": agent.full_name,
            "email": agent.email,
            "commission_percentage": float(agent.commission_percentage or 0),
        },
    )
    await db.commit()

    return _to_response(agent)


# ─── Update Agent ─────────────────────────────────────────────────────────────

@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: int,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Update an existing agent's profile and operational settings."""
    agent = await _get_agent_or_404(db, agent_id)

    update_data = body.model_dump(exclude_unset=True)

    # Unique phone enforcement
    if "phone_number" in update_data and update_data["phone_number"] != agent.phone_number:
        existing_phone = await db.execute(
            select(User).where(
                User.phone_number == update_data["phone_number"],
                User.deleted_at.is_(None),
                User.id != agent_id,
            )
        )
        if existing_phone.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This phone number is already registered.",
            )

    # Map account_status string to enum
    if "account_status" in update_data:
        update_data["account_status"] = AccountStatus(update_data["account_status"])

    for key, value in update_data.items():
        setattr(agent, key, value)

    await db.commit()
    await db.refresh(agent)

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="UPDATE_AGENT",
        entity_type="User",
        entity_id=str(agent.id),
        details={k: float(v) if hasattr(v, '__float__') and not isinstance(v, bool) else str(v) for k, v in update_data.items()},
    )
    await db.commit()

    return _to_response(agent)


# ─── Delete Agent (soft) ──────────────────────────────────────────────────────

@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Soft-delete an agent."""
    agent = await _get_agent_or_404(db, agent_id)

    agent.deleted_at = get_ist_now()
    agent.is_active = False
    agent.google_id = None
    if agent.email:
        agent.email = f"{agent.email}.deleted.{int(get_ist_now().timestamp())}"
    if agent.phone_number:
        agent.phone_number = f"{agent.phone_number}.deleted.{int(get_ist_now().timestamp())}"
    await db.commit()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="DELETE_AGENT",
        entity_type="User",
        entity_id=str(agent.id),
        details={"full_name": agent.full_name, "email": agent.email},
    )
    await db.commit()

    return None


# ─── Reset Agent Password ────────────────────────────────────────────────────

@router.post("/{agent_id}/reset-password", status_code=status.HTTP_200_OK)
async def reset_agent_password(
    agent_id: int,
    body: AgentResetPassword,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Admin-initiated password reset for an agent."""
    agent = await _get_agent_or_404(db, agent_id)

    agent.password_hash = get_password_hash(body.new_password)
    await db.commit()

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="RESET_AGENT_PASSWORD",
        entity_type="User",
        entity_id=str(agent.id),
        details={"full_name": agent.full_name},
    )
    await db.commit()

    return {"message": f"Password reset successfully for {agent.full_name}."}


# ─── Toggle Agent Status ─────────────────────────────────────────────────────

@router.post("/{agent_id}/toggle-status", response_model=AgentResponse)
async def toggle_agent_status(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Toggle agent between ACTIVE and BLOCKED."""
    agent = await _get_agent_or_404(db, agent_id)

    if agent.account_status == AccountStatus.BLOCKED:
        agent.account_status = AccountStatus.ACTIVE
        agent.is_active = True
        new_status = "ACTIVE"
    else:
        agent.account_status = AccountStatus.BLOCKED
        agent.is_active = False
        new_status = "BLOCKED"

    await db.commit()
    await db.refresh(agent)

    await log_action(
        db=db,
        user_id=current_admin.id,
        action="TOGGLE_AGENT_STATUS",
        entity_type="User",
        entity_id=str(agent.id),
        details={"full_name": agent.full_name, "new_status": new_status},
    )
    await db.commit()

    return _to_response(agent)


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _get_agent_or_404(db: AsyncSession, agent_id: int) -> User:
    """Fetch an agent by ID or raise 404."""
    result = await db.execute(
        select(User).where(
            User.id == agent_id,
            User.role == UserRole.AGENT,
            User.deleted_at.is_(None),
        )
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found.",
        )
    return agent


async def _compute_agent_metrics(db: AsyncSession, agent_id: int) -> AgentBookingMetrics:
    """Compute real booking metrics for an agent via SQL aggregation."""
    paid_statuses = (BookingStatus.FULLY_PAID, BookingStatus.PARTIAL_PAID)
    result = await db.execute(
        select(
            func.count(Booking.id).label("total"),
            func.count(case((Booking.status.in_(paid_statuses), 1))).label("confirmed"),
            func.count(case((Booking.status == BookingStatus.CANCELLED, 1))).label("cancelled"),
            func.count(case((Booking.status == BookingStatus.PENDING, 1))).label("pending"),
            func.coalesce(
                func.sum(case((Booking.status.in_(paid_statuses), Booking.total_amount))),
                0,
            ).label("revenue"),
            func.coalesce(
                func.sum(case((Booking.status.in_(paid_statuses), Booking.agent_commission))),
                0,
            ).label("commission"),
        ).where(
            Booking.agent_id == agent_id,
            Booking.deleted_at.is_(None),
        )
    )
    row = result.one()
    return AgentBookingMetrics(
        total_bookings=row.total or 0,
        confirmed_bookings=row.confirmed or 0,
        cancelled_bookings=row.cancelled or 0,
        pending_bookings=row.pending or 0,
        total_revenue=float(row.revenue or 0),
        total_commission=float(row.commission or 0),
    )


def _to_response(agent: User, total_bookings: int = 0) -> AgentResponse:
    """Map a User ORM instance to AgentResponse."""
    return AgentResponse(
        id=agent.id,
        full_name=agent.full_name,
        email=agent.email,
        phone_number=agent.phone_number,
        role=agent.role.value if hasattr(agent.role, 'value') else str(agent.role),
        account_status=agent.account_status.value if hasattr(agent.account_status, 'value') else str(agent.account_status),
        is_active=agent.is_active,
        commission_type=agent.commission_type or "PERCENTAGE",
        commission_percentage=agent.commission_percentage,
        commission_fixed_amount=agent.commission_fixed_amount,
        company_name=agent.company_name,
        gst_number=agent.gst_number,
        address=agent.address,
        admin_notes=agent.admin_notes,
        last_login=agent.last_login,
        total_bookings=total_bookings,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


# ─── Agent Package Quotas ───────────────────────────────────────────────────

from datetime import timedelta, date as date_type

@router.get("/{id}/quotas", response_model=List[AgentQuotaResponse])
async def get_agent_quotas(
    id: int,
    db: AsyncSession = Depends(get_db)
):
    """Fetch all packages along with their quota settings for this agent."""
    # Verify agent exists
    agent = await db.get(User, id)
    if not agent or agent.role != UserRole.AGENT or agent.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Fetch all packages
    packages_query = select(Package).where(
        Package.deleted_at.is_(None)
    ).order_by(Package.title.asc())
    packages_res = await db.execute(packages_query)
    packages = packages_res.scalars().all()

    # Fetch existing quotas for this agent
    quotas_query = select(AgentPackageQuota).where(
        AgentPackageQuota.agent_id == id
    )
    quotas_res = await db.execute(quotas_query)
    quotas = {q.package_id: q for q in quotas_res.scalars().all()}

    response = []
    for pkg in packages:
        quota = quotas.get(pkg.id)
        comm_type = quota.commission_type if (quota and quota.commission_type) else (agent.commission_type or "PERCENTAGE")
        comm_pct = quota.commission_percentage if (quota and quota.commission_percentage is not None) else agent.commission_percentage
        comm_fixed = quota.commission_fixed_amount if (quota and quota.commission_fixed_amount is not None) else agent.commission_fixed_amount

        response.append(AgentQuotaResponse(
            package_id=pkg.id,
            package_title=pkg.title,
            daily_quota=quota.daily_quota if quota else 10,
            is_allowed=quota.is_allowed if quota else True,
            commission_type=comm_type,
            commission_percentage=comm_pct,
            commission_fixed_amount=comm_fixed,
        ))
    return response


@router.put("/{id}/quotas", response_model=AgentQuotaResponse)
async def update_agent_quota(
    id: int,
    payload: AgentQuotaUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update or create quota & commission settings for a package for this agent."""
    # Verify agent exists
    agent = await db.get(User, id)
    if not agent or agent.role != UserRole.AGENT or agent.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify package exists
    package = await db.get(Package, payload.package_id)
    if not package or package.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Package not found")

    # Check if a quota record already exists
    quota_query = select(AgentPackageQuota).where(
        AgentPackageQuota.agent_id == id,
        AgentPackageQuota.package_id == payload.package_id
    )
    quota_res = await db.execute(quota_query)
    quota = quota_res.scalar_one_or_none()

    comm_type = payload.commission_type or "PERCENTAGE"

    if not quota:
        quota = AgentPackageQuota(
            agent_id=id,
            package_id=payload.package_id,
            daily_quota=payload.daily_quota,
            is_allowed=payload.is_allowed,
            commission_type=comm_type,
            commission_percentage=payload.commission_percentage,
            commission_fixed_amount=payload.commission_fixed_amount,
        )
        db.add(quota)
    else:
        quota.daily_quota = payload.daily_quota
        quota.is_allowed = payload.is_allowed
        quota.commission_type = comm_type
        quota.commission_percentage = payload.commission_percentage
        quota.commission_fixed_amount = payload.commission_fixed_amount

    await db.commit()
    await db.refresh(quota)

    # Invalidate package cache so agent gets the fresh commission and quota rules instantly
    from app.utils.cache import clear_cache_prefix
    clear_cache_prefix(f"packages:detail:{package.slug}")

    # Broadcast real-time quota update to the specific package channel
    from app.utils.sse import sse_manager
    import time
    
    await sse_manager.broadcast_event(
        "package", 
        str(payload.package_id),
        "QUOTA_UPDATE", 
        {
            "version": int(time.time() * 1000),
            "agent_id": id,
            "package_id": payload.package_id,
            "daily_quota": quota.daily_quota,
            "is_allowed": quota.is_allowed,
            "commission_type": quota.commission_type,
            "commission_percentage": float(quota.commission_percentage or 0) if quota.commission_percentage is not None else None,
            "commission_fixed_amount": float(quota.commission_fixed_amount or 0) if quota.commission_fixed_amount is not None else None,
        }
    )

    return AgentQuotaResponse(
        package_id=quota.package_id,
        package_title=package.title,
        daily_quota=quota.daily_quota,
        is_allowed=quota.is_allowed,
        commission_type=quota.commission_type or "PERCENTAGE",
        commission_percentage=quota.commission_percentage,
        commission_fixed_amount=quota.commission_fixed_amount,
    )


@router.get("/{id}/quotas/usage")
async def get_agent_quotas_usage(
    id: int,
    start_date: Optional[date_type] = Query(None),
    end_date: Optional[date_type] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """Fetch daywise booking summary and quota limits for the agent."""
    # Verify agent exists
    agent = await db.get(User, id)
    if not agent or agent.role != UserRole.AGENT or agent.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Agent not found")

    from app.core.timezone import get_ist_now
    if not start_date:
        start_date = get_ist_now().date()
    if not end_date:
        end_date = start_date + timedelta(days=14)

    # Fetch all packages
    packages_query = select(Package).where(
        Package.deleted_at.is_(None)
    ).order_by(Package.title.asc())
    packages_res = await db.execute(packages_query)
    packages = packages_res.scalars().all()

    # Fetch existing quotas for this agent
    quotas_query = select(AgentPackageQuota).where(
        AgentPackageQuota.agent_id == id
    )
    quotas_res = await db.execute(quotas_query)
    quotas = {q.package_id: q for q in quotas_res.scalars().all()}

    # Query bookings by this agent between start_date and end_date
    from app.models.package import PackageVariant
    from sqlalchemy.orm import selectinload
    
    bookings_query = (
        select(Booking)
        .join(PackageVariant, PackageVariant.id == Booking.variant_id)
        .options(
            selectinload(Booking.passengers),
            selectinload(Booking.package_variant)
        )
        .where(
            Booking.agent_id == id,
            Booking.travel_date >= start_date,
            Booking.travel_date <= end_date,
            Booking.status.in_((BookingStatus.FULLY_PAID, BookingStatus.PARTIAL_PAID)),
            Booking.deleted_at.is_(None)
        )
    )
    bookings_res = await db.execute(bookings_query)
    bookings = bookings_res.scalars().all()
    
    # Group bookings by (date, package_id)
    usage_map = {}
    for b in bookings:
        if not b.package_variant:
            continue
        pkg_id = b.package_variant.package_id
        key = (b.travel_date, pkg_id)
        if key not in usage_map:
            usage_map[key] = []
        usage_map[key].append(b)

    # Build response: day by day, for each package
    response = []
    current_date = start_date
    while current_date <= end_date:
        day_packages = []
        for pkg in packages:
            quota = quotas.get(pkg.id)
            daily_limit = quota.daily_quota if quota else 10
            allowed = quota.is_allowed if quota else True
            
            day_bookings = usage_map.get((current_date, pkg.id), [])
            booked_pax = sum(b.adult_count + b.child_count + b.student_count for b in day_bookings)
            
            booking_details = []
            for b in day_bookings:
                cust_name = next((p.full_name for p in b.passengers if p.is_primary), None) or (b.passengers[0].full_name if b.passengers else "Guest")
                booking_details.append({
                    "id": b.id,
                    "public_id": b.public_id,
                    "customer_name": cust_name,
                    "passenger_count": b.adult_count + b.child_count + b.student_count,
                    "status": b.status.value if hasattr(b.status, "value") else str(b.status),
                    "total_amount": float(b.total_amount)
                })

            day_packages.append({
                "package_id": pkg.id,
                "package_title": pkg.title,
                "daily_quota": daily_limit,
                "is_allowed": allowed,
                "booked_passengers": booked_pax,
                "bookings": booking_details
            })
        response.append({
            "date": current_date.isoformat(),
            "packages": day_packages
        })
        current_date += timedelta(days=1)

    return response

