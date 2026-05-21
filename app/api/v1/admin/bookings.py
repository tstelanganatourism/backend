from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import selectinload
from typing import Optional, List
from decimal import Decimal
from pydantic import BaseModel, Field

from app.db.session import get_db
from app.middleware.auth import require_admin
from app.models.booking import Booking, BookingPassenger
from app.models.package import PackageVariant, Package
from app.models.room import RoomVariant, Room
from app.models.user import User
from app.utils.pricing import get_effective_package_prices

router = APIRouter(
    prefix="/bookings",
    tags=["Admin - Bookings"],
    dependencies=[Depends(require_admin)],
)


@router.get("")
async def list_admin_bookings(
    db: AsyncSession = Depends(get_db),
    search: Optional[str] = Query(None, description="Search by booking ID, customer name or email"),
    status_filter: Optional[str] = Query(None, description="Filter by booking status"),
    source_filter: Optional[str] = Query(None, description="Filter by source: PUBLIC or AGENT"),
    target_filter: Optional[str] = Query(None, description="Filter by target: PACKAGE or ROOM"),
    agent_id: Optional[int] = Query(None, description="Filter by Agent ID"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    Paginated admin booking listing. Never exposes commission data.
    """
    query = (
        select(Booking)
        .options(
            selectinload(Booking.passengers),
        )
        .where(Booking.deleted_at.is_(None))
        .order_by(Booking.created_at.desc())
    )

    if agent_id is not None:
        query = query.where(Booking.agent_id == agent_id)

    if status_filter:
        from app.models.enums import BookingStatus
        try:
            status_enum = BookingStatus(status_filter.upper())
            query = query.where(Booking.status == status_enum)
        except ValueError:
            pass

    if source_filter:
        from app.models.enums import BookingSource
        try:
            source_enum = BookingSource(source_filter.upper())
            query = query.where(Booking.source == source_enum)
        except ValueError:
            pass

    if target_filter:
        if target_filter.upper() == "ROOM":
            query = query.where(Booking.room_variant_id.isnot(None))
        elif target_filter.upper() == "PACKAGE":
            query = query.where(Booking.variant_id.isnot(None))

    if start_date:
        try:
            from datetime import datetime
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            query = query.where(Booking.travel_date >= start_dt)
        except ValueError:
            pass
            
    if end_date:
        try:
            from datetime import datetime
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
            query = query.where(Booking.travel_date <= end_dt)
        except ValueError:
            pass

    if search:
        s = f"%{search}%"
        # Subquery: find user IDs matching name or email
        user_id_subq = (
            select(User.id)
            .where(
                or_(
                    User.full_name.ilike(s),
                    User.email.ilike(s),
                    User.phone_number.ilike(s),
                )
            )
            .scalar_subquery()
        )
        
        # Subquery: find booking IDs matching the primary passenger's name
        booking_id_subq = (
            select(BookingPassenger.booking_id)
            .where(
                and_(
                    BookingPassenger.is_primary == True,
                    BookingPassenger.full_name.ilike(s)
                )
            )
            .scalar_subquery()
        )
        
        # Explicitly support 'guest' keyword for public checkouts
        guest_condition = []
        if search.lower() == "guest":
            guest_condition = [Booking.user_id.is_(None)]

        query = query.where(
            or_(
                Booking.public_id.ilike(s),
                Booking.user_id.in_(user_id_subq),
                Booking.id.in_(booking_id_subq),
                *guest_condition
            )
        )

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar_one() or 0

    # Paginate
    paginated_query = query.limit(limit).offset(offset)
    result = await db.execute(paginated_query)
    bookings = result.scalars().all()

    # Collect agent + user IDs for batch lookup
    agent_ids = {b.agent_id for b in bookings if b.agent_id}
    user_ids = {b.user_id for b in bookings if b.user_id}
    all_user_ids = agent_ids | user_ids

    user_map: dict = {}
    if all_user_ids:
        u_res = await db.execute(select(User).where(User.id.in_(all_user_ids)))
        for u in u_res.scalars().all():
            user_map[u.id] = u

    # Collect package / room variant IDs
    variant_ids = {b.variant_id for b in bookings if b.variant_id}
    room_variant_ids = {b.room_variant_id for b in bookings if b.room_variant_id}

    variant_map: dict = {}
    if variant_ids:
        pv_res = await db.execute(
            select(PackageVariant, Package.title)
            .join(Package, Package.id == PackageVariant.package_id)
            .where(PackageVariant.id.in_(variant_ids))
        )
        for pv, pkg_title in pv_res.all():
            variant_map[pv.id] = {"package_title": pkg_title, "variant_title": pv.title}

    room_variant_map: dict = {}
    if room_variant_ids:
        rv_res = await db.execute(
            select(RoomVariant, Room.lodge_name)
            .join(Room, Room.id == RoomVariant.room_id)
            .where(RoomVariant.id.in_(room_variant_ids))
        )
        for rv, room_name in rv_res.all():
            room_variant_map[rv.id] = {"package_title": room_name, "variant_title": rv.variant_name}

    items = []
    for b in bookings:
        customer = user_map.get(b.user_id) if b.user_id else None
        agent = user_map.get(b.agent_id) if b.agent_id else None

        if b.variant_id and b.variant_id in variant_map:
            title_info = variant_map[b.variant_id]
        elif b.room_variant_id and b.room_variant_id in room_variant_map:
            title_info = room_variant_map[b.room_variant_id]
        else:
            title_info = {"package_title": "—", "variant_title": "—"}

        items.append({
            "id": b.id,
            "public_id": b.public_id,
            "target_type": "ROOM" if b.room_variant_id else "PACKAGE",
            "source": b.source.value if hasattr(b.source, "value") else str(b.source),
            "status": b.status.value if hasattr(b.status, "value") else str(b.status),
            "travel_date": b.travel_date.isoformat(),
            "adult_count": b.adult_count,
            "child_count": b.child_count,
            # Public pricing — never expose agent_commission or pricing_snapshot
            "subtotal_amount": float(b.subtotal_amount),
            "coupon_discount": float(b.coupon_discount),
            "coupon_applied": b.coupon_applied,
            "gst_amount": float(b.gst_amount),
            "gateway_fee": float(b.gateway_fee),
            "total_amount": float(b.total_amount),
            "paid_amount": float(b.paid_amount),
            "remaining_balance": float(b.remaining_balance),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "package_title": title_info["package_title"],
            "variant_title": title_info["variant_title"],
            "customer": {
                "id": customer.id if customer else None,
                "full_name": customer.full_name if customer else "Guest",
                "email": customer.email if customer else None,
            },
            "agent": {
                "id": agent.id if agent else None,
                "full_name": agent.full_name if agent else None,
            } if agent else None,
            "passenger_count": len(b.passengers),
            "primary_passenger_name": (
                next((p.full_name for p in b.passengers if p.is_primary), None)
                or (b.passengers[0].full_name if b.passengers else None)
            ),
        })

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/summary")
async def get_bookings_summary(
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns aggregated booking KPIs for the admin bookings page header.
    Uses raw DB enum string values to avoid alias confusion.
    """
    from sqlalchemy import case, literal
    
    base_cond = [Booking.deleted_at.is_(None)]
    
    if start_date:
        try:
            from datetime import datetime
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            base_cond.append(Booking.travel_date >= start_dt)
        except ValueError:
            pass
            
    if end_date:
        try:
            from datetime import datetime
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
            base_cond.append(Booking.travel_date <= end_dt)
        except ValueError:
            pass

    # Use raw string values since BookingStatus.CONFIRMED is an alias for 'FULLY_PAID'
    result = await db.execute(
        select(
            func.count(Booking.id).label("total"),
            func.sum(
                case(
                    (Booking.status == "FULLY_PAID", literal(1)),
                    else_=literal(0),
                )
            ).label("confirmed"),
            func.sum(
                case(
                    (Booking.status == "PENDING", literal(1)),
                    else_=literal(0),
                )
            ).label("pending"),
            func.sum(
                case(
                    (Booking.status == "FULLY_PAID", Booking.total_amount),
                    else_=literal(0),
                )
            ).label("revenue"),
        ).where(and_(*base_cond))
    )
    row = result.one()

    return {
        "total": row.total or 0,
        "confirmed": int(row.confirmed or 0),
        "pending": int(row.pending or 0),
        "revenue": float(row.revenue or 0),
    }


# ─── Admin Direct Booking ────────────────────────────────────────────────────

class AdminPassengerInput(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=200)
    age: int = Field(..., ge=0, le=150)
    gender: Optional[str] = None
    phone: Optional[str] = None
    aadhaar: Optional[str] = None
    relationship: Optional[str] = None
    is_primary: Optional[bool] = False

class AdminCreateBookingRequest(BaseModel):
    target_type: str  # 'package' or 'room'
    travel_date: str
    quantity: int = Field(..., ge=1)
    variant_id: Optional[int] = None
    room_variant_id: Optional[int] = None
    adult_count: Optional[int] = None
    child_count: Optional[int] = None
    user_id: Optional[int] = None  # optional: assign to a registered user
    agent_id: Optional[int] = None  # optional: assign under an agent
    passengers: List[AdminPassengerInput] = []
    # Room-specific
    departure_date: Optional[str] = None
    slot_start: Optional[str] = None
    slot_end: Optional[str] = None

@router.post("/create")
async def admin_create_booking(
    request: AdminCreateBookingRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """
    Admin direct booking — bypasses Razorpay completely.
    Creates a CONFIRMED booking, locks inventory directly.
    """
    import uuid
    from datetime import date, timedelta, time
    from app.models.package import PackageVariantInventory
    from app.models.room import RoomSlotInventory
    from app.models.enums import BookingStatus, BookingSource, GenderType
    from app.core.security import AadharCryptography, AadharHashing
    from app.core.timezone import get_ist_now
    from app.utils.verhoeff import is_valid_aadhaar

    travel_date = date.fromisoformat(request.travel_date)
    adult_count = request.adult_count or request.quantity
    child_count = request.child_count or 0
    subtotal_amount = Decimal("0.00")
    room_variant_id_val = None
    package_variant_id_val = None

    if request.target_type == 'package':
        if not request.variant_id:
            raise HTTPException(status_code=400, detail="variant_id is required for package booking")
        
        variant_result = await db.execute(
            select(PackageVariant).where(PackageVariant.id == request.variant_id)
        )
        variant = variant_result.scalar_one_or_none()
        if not variant:
            raise HTTPException(status_code=404, detail="Package variant not found")
        
        package_variant_id_val = variant.id

        # Lock inventory directly (booked_count, not reserved_count)
        inv_query = select(PackageVariantInventory).where(
            PackageVariantInventory.variant_id == request.variant_id,
            PackageVariantInventory.date == travel_date
        ).with_for_update()
        inv_res = await db.execute(inv_query)
        inv = inv_res.scalar_one_or_none()
        if not inv:
            raise HTTPException(status_code=400, detail=f"No inventory found for date {travel_date}")
        if inv.is_closed:
            raise HTTPException(status_code=400, detail=f"Date {travel_date} is closed for bookings")
        available = inv.total_capacity - inv.booked_count - inv.reserved_count
        if available < request.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient seats. Available: {available}")
        
        # Calculate subtotal using precise Decimal representation and effective prices helper
        eff_adult, eff_child = get_effective_package_prices(
            variant.adult_price, variant.child_price, inv.price_override
        )
        subtotal_amount = (Decimal(str(eff_adult)) * adult_count) + \
                          (Decimal(str(eff_child)) * child_count)
        
        inv.booked_count += request.quantity

    elif request.target_type == 'room':
        if not request.room_variant_id:
            raise HTTPException(status_code=400, detail="room_variant_id is required for room booking")
        
        rv_result = await db.execute(
            select(RoomVariant).where(RoomVariant.id == request.room_variant_id)
        )
        rv = rv_result.scalar_one_or_none()
        if not rv:
            raise HTTPException(status_code=404, detail="Room variant not found")
        
        room_variant_id_val = rv.id
        
        # Calculate stay dates
        arrival = travel_date
        departure = date.fromisoformat(request.departure_date) if request.departure_date else (arrival + timedelta(days=1))
        stay_dates = []
        current = arrival
        while current < departure:
            stay_dates.append(current)
            current += timedelta(days=1)
        
        from app.services.room_calculation import calculate_required_rooms
        required_rooms = calculate_required_rooms(request.quantity, rv.capacity_per_room)
        
        slot_start_t = time.fromisoformat(request.slot_start) if request.slot_start else None
        slot_end_t = time.fromisoformat(request.slot_end) if request.slot_end else None

        # Check & lock all dates
        for sd in stay_dates:
            inv_query = select(RoomSlotInventory).where(
                RoomSlotInventory.room_variant_id == request.room_variant_id,
                RoomSlotInventory.date == sd,
                RoomSlotInventory.slot_start == slot_start_t,
                RoomSlotInventory.slot_end == slot_end_t
            ).with_for_update()
            inv_res = await db.execute(inv_query)
            room_inv = inv_res.scalar_one_or_none()
            if not room_inv:
                raise HTTPException(status_code=400, detail=f"No inventory for date {sd}")
            if room_inv.is_closed:
                raise HTTPException(status_code=400, detail=f"Date {sd} is closed")
            available_rooms = room_inv.total_rooms - room_inv.booked_rooms - room_inv.reserved_rooms
            if available_rooms < required_rooms:
                raise HTTPException(status_code=400, detail=f"Insufficient rooms on {sd}. Available: {available_rooms}, Need: {required_rooms}")
            room_inv.booked_rooms += required_rooms

        # Price calculation: use weekday/weekend per date
        total_price = Decimal("0.00")
        for sd in stay_dates:
            is_weekend = sd.weekday() >= 5
            price = Decimal(str(rv.weekend_price)) if is_weekend else Decimal(str(rv.weekday_price))
            total_price += price * required_rooms
        subtotal_amount = total_price
    else:
        raise HTTPException(status_code=400, detail="Invalid target_type. Must be 'package' or 'room'.")

    # Admin bookings: no GST, no gateway fee, no coupon
    gst_amount = Decimal("0.00")
    gateway_fee = Decimal("0.00")
    total_amount = subtotal_amount

    # Agent commission (if booking under an agent)
    agent_commission = Decimal("0.00")
    agent_id_val = request.agent_id

    pricing_snapshot = {
        "subtotal_amount": str(subtotal_amount),
        "coupon_discount": "0.00",
        "coupon_applied": None,
        "gst_amount": "0.00",
        "gateway_fee": "0.00",
        "tourist_total": str(total_amount),
        "agent_discount": "0.00",
        "agent_payable": str(total_amount),
        "payment_method": "ADMIN_MANUAL",
        "created_by_admin_id": current_admin.id,
        "admin_name": current_admin.full_name,
    }

    booking = Booking(
        public_id="BK-" + str(uuid.uuid4())[:8].upper(),
        user_id=request.user_id,
        agent_id=agent_id_val,
        source=BookingSource.ADMIN_DIRECT,
        variant_id=package_variant_id_val,
        room_variant_id=room_variant_id_val,
        travel_date=travel_date,
        adult_count=adult_count,
        child_count=child_count,
        subtotal_amount=subtotal_amount,
        coupon_discount=Decimal("0.00"),
        coupon_applied=None,
        gst_amount=gst_amount,
        gateway_fee=gateway_fee,
        total_amount=total_amount,
        paid_amount=Decimal("0.00"),
        remaining_balance=Decimal("0.00"),
        agent_commission=agent_commission,
        status=BookingStatus.CONFIRMED,
        pricing_snapshot=pricing_snapshot,
    )
    db.add(booking)
    await db.flush()

    # Persist passengers
    crypto = AadharCryptography()
    for p_data in request.passengers:
        gender_enum = None
        if p_data.gender:
            try:
                gender_enum = GenderType(p_data.gender.upper())
            except (ValueError, KeyError):
                pass

        raw_aadhaar = (p_data.aadhaar or '').strip()
        encrypted = crypto.encrypt(raw_aadhaar) if raw_aadhaar else None
        hashed = AadharHashing.hash_aadhar(raw_aadhaar) if raw_aadhaar else None

        passenger = BookingPassenger(
            booking_id=booking.id,
            full_name=p_data.full_name,
            age=p_data.age,
            gender=gender_enum,
            phone_number=p_data.phone,
            relationship_to_lead=p_data.relationship,
            is_primary=p_data.is_primary or False,
            aadhar_encrypted=encrypted,
            aadhar_hash=hashed,
        )
        db.add(passenger)

    # Persist stay dates for room bookings
    if request.target_type == 'room':
        from app.models.booking import BookingStayDate
        for sd in stay_dates:
            db.add(BookingStayDate(booking_id=booking.id, date=sd))

    await db.commit()

    return {
        "status": "success",
        "public_id": booking.public_id,
        "message": "Booking created directly by admin. No payment required.",
    }


# ─── Admin PDF Manual Trigger & Cancellations ───────────────────────────────

class AdminCancelBookingPayload(BaseModel):
    status: str  # APPROVED, REJECTED, REFUNDED
    admin_notes: Optional[str] = None

@router.post("/{id}/generate-ticket")
async def admin_generate_ticket(
    id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Manually triggers ticket PDF generation for the given booking ID.
    """
    booking = await db.get(Booking, id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    try:
        from app.worker import get_arq_pool
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job("generate_booking_ticket_task", booking.id)
        return {
            "status": "success",
            "message": f"Ticket PDF generation successfully queued for booking {booking.public_id}."
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to enqueue ticket generation task: {str(e)}"
        )

@router.post("/{id}/generate-invoice")
async def admin_generate_invoice(
    id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Manually triggers invoice PDF generation for the given booking ID.
    """
    booking = await db.get(Booking, id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    try:
        from app.worker import get_arq_pool
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job("generate_booking_invoice_task", booking.id)
        return {
            "status": "success",
            "message": f"Invoice PDF generation successfully queued for booking {booking.public_id}."
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to enqueue invoice generation task: {str(e)}"
        )

@router.patch("/{id}/cancel")
async def admin_cancel_booking(
    id: int,
    payload: AdminCancelBookingPayload,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Process tourist cancellation request:
    1. Verify status parameter (APPROVED, REJECTED)
    2. Check booking and its cancellation requests
    3. Perform calculations: deduct 35% fee if APPROVED and set refund_amount. Refund is manual.
    4. Release reserved seats/rooms back to inventories if booking is APPROVED.
    """
    from app.models.enums import BookingStatus, CancellationStatus
    from app.models.booking import CancellationRequest
    from loguru import logger
    from datetime import date, timedelta, time

    booking = await db.get(Booking, id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Find the cancellation request for this booking
    cancel_stmt = select(CancellationRequest).where(
        CancellationRequest.booking_id == booking.id
    ).order_by(CancellationRequest.created_at.desc()).limit(1)
    cancel_res = await db.execute(cancel_stmt)
    cancellation_req = cancel_res.scalar_one_or_none()

    target_status = payload.status.upper()
    if target_status not in ["APPROVED", "REJECTED"]:
        raise HTTPException(status_code=400, detail="Invalid cancellation status. Must be APPROVED or REJECTED.")

    if target_status == "REJECTED":
        if cancellation_req:
            cancellation_req.status = CancellationStatus.REJECTED
            cancellation_req.processed_at = func.now()
            cancellation_req.processed_by = current_admin.id
            cancellation_req.admin_notes = payload.admin_notes
        else:
            cancellation_req = CancellationRequest(
                booking_id=booking.id,
                reason="Admin manual rejection",
                status=CancellationStatus.REJECTED,
                processed_at=func.now(),
                processed_by=current_admin.id,
                admin_notes=payload.admin_notes
            )
            db.add(cancellation_req)
        await db.commit()
        return {"status": "success", "message": "Cancellation request has been rejected."}

    # APPROVED cancellation
    if booking.status == BookingStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Booking is already cancelled.")

    # Calculate 35% cancellation fee based on total_amount
    cancellation_fee = (booking.total_amount * Decimal("0.35")).quantize(Decimal("0.01"))
    # refund_amount = paid_amount - cancellation_fee (refund remains manual)
    paid_amount = Decimal(str(booking.paid_amount or 0.00))
    refund_amount = max(Decimal("0.00"), paid_amount - cancellation_fee).quantize(Decimal("0.01"))

    # Release reserved seats/rooms back to availability pool
    if booking.variant_id:
        from app.models.package import PackageVariantInventory
        inv_stmt = select(PackageVariantInventory).where(
            PackageVariantInventory.variant_id == booking.variant_id,
            PackageVariantInventory.date == booking.travel_date
        ).with_for_update()
        inv_res = await db.execute(inv_stmt)
        inv = inv_res.scalar_one_or_none()
        if inv:
            quantity = booking.adult_count + booking.child_count
            inv.booked_count = max(0, inv.booked_count - quantity)
            logger.info(f"Released {quantity} seats for package variant inventory {booking.variant_id} on {booking.travel_date}")

    elif booking.room_variant_id:
        # 1. Fetch stay dates
        from app.models.booking import BookingStayDate
        dates_res = await db.execute(
            select(BookingStayDate.date).where(BookingStayDate.booking_id == booking.id)
        )
        stay_dates = [row[0] for row in dates_res.all()]
        
        # 2. Fetch standard slot_start / slot_end from the parent room
        from app.models.room import RoomVariant, Room
        rv_res = await db.execute(
            select(RoomVariant, Room.slot_start, Room.slot_end)
            .join(Room, Room.id == RoomVariant.room_id)
            .where(RoomVariant.id == booking.room_variant_id)
        )
        rv_row = rv_res.first()
        if rv_row:
            rv, slot_start, slot_end = rv_row
            from app.services.room_calculation import calculate_required_rooms
            total_qty = booking.adult_count + booking.child_count
            required_rooms = calculate_required_rooms(total_qty, rv.capacity_per_room)
            
            for stay_date in stay_dates:
                from app.models.room import RoomSlotInventory
                inv_stmt = select(RoomSlotInventory).where(
                    RoomSlotInventory.room_variant_id == booking.room_variant_id,
                    RoomSlotInventory.date == stay_date,
                    RoomSlotInventory.slot_start == slot_start,
                    RoomSlotInventory.slot_end == slot_end
                ).with_for_update()
                inv_res = await db.execute(inv_stmt)
                inv = inv_res.scalar_one_or_none()
                if inv:
                    inv.booked_rooms = max(0, inv.booked_rooms - required_rooms)
                    logger.info(f"Released {required_rooms} rooms for room variant inventory {booking.room_variant_id} on {stay_date}")

    # Update states
    booking.status = BookingStatus.CANCELLED
    
    # Store cancellation request record with refund details
    if cancellation_req:
        cancellation_req.status = CancellationStatus.APPROVED
        cancellation_req.processed_at = func.now()
        cancellation_req.processed_by = current_admin.id
        cancellation_req.admin_notes = payload.admin_notes
        cancellation_req.cancellation_fee = cancellation_fee
        cancellation_req.refund_amount = refund_amount
    else:
        cancellation_req = CancellationRequest(
            booking_id=booking.id,
            reason="Admin manual cancellation via WhatsApp",
            status=CancellationStatus.APPROVED,
            processed_at=func.now(),
            processed_by=current_admin.id,
            admin_notes=payload.admin_notes,
            cancellation_fee=cancellation_fee,
            refund_amount=refund_amount
        )
        db.add(cancellation_req)

    await db.commit()

    return {
        "status": "success",
        "message": f"Booking {booking.public_id} successfully cancelled. Cancellation fee of Rs. {cancellation_fee} applied. Manual refund of Rs. {refund_amount} required.",
        "cancellation_fee": float(cancellation_fee),
        "refund_amount": float(refund_amount)
    }


@router.get("/cancellation-requests")
async def list_cancellation_requests(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    Lists all pending and processed cancellation requests.
    """
    from app.models.booking import CancellationRequest
    
    stmt = (
        select(CancellationRequest)
        .options(selectinload(CancellationRequest.booking).selectinload(Booking.passengers))
        .order_by(CancellationRequest.requested_at.desc())
        .limit(limit)
        .offset(offset)
    )
    
    result = await db.execute(stmt)
    requests = result.scalars().all()
    
    items = []
    for r in requests:
        booking = r.booking
        if not booking:
            continue
        lead_passenger = next((p.full_name for p in booking.passengers if p.is_primary), None) or (booking.passengers[0].full_name if booking.passengers else "Guest")
        
        items.append({
            "id": r.id,
            "booking_id": booking.id,
            "booking_public_id": booking.public_id,
            "customer_name": lead_passenger,
            "travel_date": booking.travel_date.isoformat(),
            "total_amount": float(booking.total_amount),
            "paid_amount": float(booking.paid_amount),
            "reason": r.reason,
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "requested_at": r.requested_at.isoformat(),
            "processed_at": r.processed_at.isoformat() if r.processed_at else None,
            "cancellation_fee": float(r.cancellation_fee) if r.cancellation_fee is not None else None,
            "refund_amount": float(r.refund_amount) if r.refund_amount is not None else None,
            "admin_notes": r.admin_notes
        })
        
    return items


@router.post("/{id}/mark-balance-paid")
async def admin_mark_balance_paid(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """
    Admin manually marks the remaining balance of a booking as paid.
    """
    from app.models.enums import BookingStatus, PaymentStatus
    from app.models.payment import Payment
    import uuid

    # 1. Fetch booking with locking
    booking = await db.get(Booking, id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status != BookingStatus.PARTIAL_PAID:
        raise HTTPException(
            status_code=400,
            detail="Only partially paid bookings can be marked as balance paid"
        )

    if booking.remaining_balance <= Decimal("0.01"):
        raise HTTPException(status_code=400, detail="No remaining balance to pay")

    # 2. Count captured payments (ensure limit of 1 initial + 1 balance)
    captured_stmt = select(func.count(Payment.id)).where(
        Payment.booking_id == booking.id,
        Payment.status == PaymentStatus.CAPTURED
    )
    captured_res = await db.execute(captured_stmt)
    captured_count = captured_res.scalar_one() or 0

    if captured_count >= 2:
        raise HTTPException(status_code=400, detail="Maximum payment records reached for this booking")

    # 3. Create manual payment record
    manual_payment = Payment(
        booking_id=booking.id,
        razorpay_order_id=f"man_{booking.public_id}_{uuid.uuid4().hex[:6].upper()}",
        razorpay_payment_id=f"pay_man_{uuid.uuid4().hex[:10].upper()}",
        amount=booking.remaining_balance,
        status=PaymentStatus.CAPTURED,
        payment_method="ADMIN_MANUAL",
    )
    db.add(manual_payment)

    # 4. Update booking amounts and status
    booking.paid_amount = booking.total_amount
    booking.remaining_balance = Decimal("0.00")
    booking.status = BookingStatus.FULLY_PAID

    await db.flush()

    # 5. Queue ticket and invoice generation tasks
    try:
        from app.worker import get_arq_pool
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job("generate_booking_ticket_task", booking.id)
        await arq_pool.enqueue_job("generate_booking_invoice_task", booking.id)
    except Exception as arq_err:
        logger.warning(f"Failed to enqueue PDF tasks for admin balance payment: {arq_err}")

    await db.commit()

    return {
        "status": "success",
        "message": f"Booking {booking.public_id} successfully marked as FULLY_PAID. PDFs queued for generation.",
    }