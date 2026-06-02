from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import selectinload
from typing import Optional, List
from decimal import Decimal
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db.session import get_db
from app.middleware.auth import require_admin
from app.models.booking import Booking, BookingPassenger, BookingStayDate
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
    variant_id: Optional[int] = Query(None, description="Filter by Package Variant ID"),
    room_variant_id: Optional[int] = Query(None, description="Filter by Room Variant ID"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    Paginated admin booking listing. Never exposes commission data.
    """
    base_query = select(Booking).where(Booking.deleted_at.is_(None))

    if agent_id is not None:
        base_query = base_query.where(Booking.agent_id == agent_id)

    if status_filter:
        from app.models.enums import BookingStatus
        try:
            status_enum = BookingStatus(status_filter.upper())
            base_query = base_query.where(Booking.status == status_enum)
        except ValueError:
            pass

    if source_filter:
        from app.models.enums import BookingSource
        try:
            source_enum = BookingSource(source_filter.upper())
            base_query = base_query.where(Booking.source == source_enum)
        except ValueError:
            pass

    if target_filter:
        if target_filter.upper() == "ROOM":
            base_query = base_query.where(Booking.room_variant_id.isnot(None))
        elif target_filter.upper() == "PACKAGE":
            base_query = base_query.where(Booking.variant_id.isnot(None))

    if variant_id is not None:
        base_query = base_query.where(Booking.variant_id == variant_id)

    if room_variant_id is not None:
        base_query = base_query.where(Booking.room_variant_id == room_variant_id)

    if start_date or end_date:
        try:
            from datetime import datetime
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
            
            conditions = []
            if start_dt and end_dt:
                conditions.append(and_(Booking.travel_date >= start_dt, Booking.travel_date <= end_dt))
                conditions.append(Booking.stay_dates.any(and_(BookingStayDate.date >= start_dt, BookingStayDate.date <= end_dt)))
            elif start_dt:
                conditions.append(Booking.travel_date >= start_dt)
                conditions.append(Booking.stay_dates.any(BookingStayDate.date >= start_dt))
            elif end_dt:
                conditions.append(Booking.travel_date <= end_dt)
                conditions.append(Booking.stay_dates.any(BookingStayDate.date <= end_dt))
                
            if conditions:
                base_query = base_query.where(or_(*conditions))
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
        )
        
        # Explicitly support 'guest' keyword for public checkouts
        guest_condition = []
        if search.lower() == "guest":
            guest_condition = [Booking.user_id.is_(None)]

        base_query = base_query.where(
            or_(
                Booking.public_id.ilike(s),
                Booking.user_id.in_(user_id_subq),
                Booking.id.in_(booking_id_subq),
                *guest_condition
            )
        )

    # Count total
    count_query = base_query.with_only_columns(func.count(Booking.id)).order_by(None)
    total_result = await db.execute(count_query)
    total = total_result.scalar_one() or 0

    # Paginate and add loaders
    paginated_query = (
        base_query
        .options(selectinload(Booking.passengers))
        .order_by(Booking.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
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
            # Admin pricing - expose agent_commission and agent_payable
            "subtotal_amount": float(b.subtotal_amount),
            "coupon_discount": float(b.coupon_discount),
            "coupon_applied": b.coupon_applied,
            "gst_amount": float(b.gst_amount),
            "gateway_fee": float(b.gateway_fee),
            "total_amount": float(b.total_amount),
            "paid_amount": float(b.paid_amount),
            "remaining_balance": float(b.remaining_balance),
            "agent_commission": float(b.agent_commission) if b.agent_commission else None,
            "agent_payable": float(b.total_amount - (b.agent_commission or 0)),
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
                    (Booking.status == "PARTIAL_PAID", literal(1)),
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
    # Payment
    amount_paid: Optional[float] = None

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
    from app.models.enums import BookingStatus, BookingSource, GenderType, PaymentStatus
    from app.models.payment import Payment
    from app.core.security import AadharCryptography, AadharHashing
    from app.core.timezone import get_ist_now
    from app.utils.ledger import recompute_booking_ledger
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

        # Admin bypass: fetch inventory if it exists — but never block on missing/closed/full
        inv_query = select(PackageVariantInventory).where(
            PackageVariantInventory.variant_id == request.variant_id,
            PackageVariantInventory.date == travel_date
        ).with_for_update()
        inv_res = await db.execute(inv_query)
        inv = inv_res.scalar_one_or_none()

        if inv is None:
            # Auto-create an inventory row on the fly so booked_count is tracked
            inv = PackageVariantInventory(
                variant_id=request.variant_id,
                date=travel_date,
                total_capacity=request.quantity,  # set to at least what admin is booking
                booked_count=0,
                reserved_count=0,
                is_closed=False,
            )
            db.add(inv)
            await db.flush()

        # Admin always goes through — just increase total_capacity if we'd go over
        if inv.booked_count + request.quantity > inv.total_capacity:
            inv.total_capacity = inv.booked_count + inv.reserved_count + request.quantity

        # Calculate subtotal using effective prices (use base variant prices if no override)
        eff_adult, eff_child = get_effective_package_prices(
            variant.adult_price, variant.child_price, inv.price_override if hasattr(inv, 'price_override') else None
        )
        subtotal_amount = (Decimal(str(eff_adult)) * adult_count) + \
                          (Decimal(str(eff_child)) * child_count)
        
        # Lock the seat in inventory
        inv.booked_count += request.quantity

    elif request.target_type == 'room':
        if not request.room_variant_id:
            raise HTTPException(status_code=400, detail="room_variant_id is required for room booking")
        
        rv_result = await db.execute(
            select(RoomVariant, Room).join(Room, Room.id == RoomVariant.room_id).where(RoomVariant.id == request.room_variant_id)
        )
        row = rv_result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Room variant not found")
        
        rv, room_obj = row
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
        
        slot_start_t = time.fromisoformat(request.slot_start) if request.slot_start else room_obj.slot_start
        slot_end_t = time.fromisoformat(request.slot_end) if request.slot_end else room_obj.slot_end

        # Admin bypass: fetch each stay date's inventory — auto-create if missing, never block
        for sd in stay_dates:
            inv_query = select(RoomSlotInventory).where(
                RoomSlotInventory.room_variant_id == request.room_variant_id,
                RoomSlotInventory.date == sd,
                RoomSlotInventory.slot_start == slot_start_t,
                RoomSlotInventory.slot_end == slot_end_t
            ).with_for_update()
            inv_res = await db.execute(inv_query)
            room_inv = inv_res.scalar_one_or_none()

            if room_inv is None:
                # Auto-create a slot inventory row on the fly
                room_inv = RoomSlotInventory(
                    room_variant_id=request.room_variant_id,
                    date=sd,
                    slot_start=slot_start_t,
                    slot_end=slot_end_t,
                    total_rooms=required_rooms,
                    booked_rooms=0,
                    reserved_rooms=0,
                    is_closed=False,
                )
                db.add(room_inv)

            # Admin always goes through — expand total_rooms if needed to keep data consistent
            if room_inv.booked_rooms + required_rooms > room_inv.total_rooms:
                room_inv.total_rooms = room_inv.booked_rooms + room_inv.reserved_rooms + required_rooms

            # Record this booking in the inventory
            room_inv.booked_rooms += required_rooms
            
        await db.flush()

        # Price calculation: use weekday/weekend per date
        total_price = Decimal("0.00")
        for sd in stay_dates:
            is_weekend = sd.weekday() >= 5
            price = Decimal(str(rv.weekend_price)) if is_weekend else Decimal(str(rv.weekday_price))
            total_price += price * required_rooms
        subtotal_amount = total_price
    else:
        raise HTTPException(status_code=400, detail="Invalid target_type. Must be 'package' or 'room'.")

    # Admin bookings: calculate GST (5%) and Gateway Fee (1%)
    gst_amount = (subtotal_amount * Decimal("0.05")).quantize(Decimal("0.01"))
    gateway_fee = ((subtotal_amount + gst_amount) * Decimal("0.01")).quantize(Decimal("0.01"))
    total_amount = subtotal_amount + gst_amount + gateway_fee

    # Agent commission (if booking under an agent)
    agent_commission = Decimal("0.00")
    agent_id_val = request.agent_id

    paid_amount_val = Decimal(str(request.amount_paid)).quantize(Decimal("0.01")) if request.amount_paid is not None else total_amount
    if paid_amount_val < Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Amount paid cannot be negative")
    if paid_amount_val > total_amount + Decimal("0.01"):
        raise HTTPException(status_code=400, detail=f"Amount paid cannot exceed total amount {total_amount}")
    paid_amount_val = min(paid_amount_val, total_amount)
    remaining_balance_val = max(Decimal("0.00"), total_amount - paid_amount_val)

    pricing_snapshot = {
        "subtotal_amount": str(subtotal_amount),
        "coupon_discount": "0.00",
        "coupon_applied": None,
        "gst_amount": str(gst_amount),
        "gateway_fee": str(gateway_fee),
        "tourist_total": str(total_amount),
        "agent_discount": "0.00",
        "agent_payable": str(total_amount),
        "payment_method": "ADMIN_MANUAL",
        "created_by_admin_id": current_admin.id,
        "admin_name": current_admin.full_name,
    }

    if request.target_type == 'room':
        if request.slot_start:
            pricing_snapshot["slot_start"] = str(request.slot_start)
        if request.slot_end:
            pricing_snapshot["slot_end"] = str(request.slot_end)


    # Determine prefix and sequence for Admin booking
    public_id_val = ""
    if request.target_type == 'room':
        seq_res = await db.execute(text("SELECT nextval('booking_seq_ac')"))
        seq_val = seq_res.scalar()
        public_id_val = f"TBT_AC_{seq_val}"
    else:
        # Determine if Boat Ride (TOUR) or Sightseeing (TRIP)
        from app.models.enums import PackageType
        pkg_res = await db.execute(
            select(Package.type)
            .join(PackageVariant, PackageVariant.package_id == Package.id)
            .where(PackageVariant.id == request.variant_id)
        )
        pkg_type = pkg_res.scalar_one_or_none()
        
        if pkg_type == PackageType.TRIP:
            seq_res = await db.execute(text("SELECT nextval('booking_seq_ss')"))
            seq_val = seq_res.scalar()
            public_id_val = f"TBT_SS_{seq_val}"
        else:
            seq_res = await db.execute(text("SELECT nextval('booking_seq_bt')"))
            seq_val = seq_res.scalar()
            public_id_val = f"TBT_BT_{seq_val}"

    booking = Booking(
        public_id=public_id_val,
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
        paid_amount=paid_amount_val,
        remaining_balance=remaining_balance_val,
        agent_commission=agent_commission,
        status=(
            BookingStatus.FULLY_PAID
            if remaining_balance_val <= Decimal("0.01")
            else BookingStatus.PARTIAL_PAID
            if paid_amount_val > Decimal("0.00")
            else BookingStatus.PENDING
        ),
        pricing_snapshot=pricing_snapshot,
    )
    db.add(booking)
    await db.flush()

    if paid_amount_val > Decimal("0.00"):
        db.add(Payment(
            booking_id=booking.id,
            payment_reference_id=f"ADMIN_{booking.public_id}_{uuid.uuid4().hex[:8].upper()}",
            razorpay_order_id=None,
            razorpay_payment_id=None,
            amount=paid_amount_val,
            status=PaymentStatus.CAPTURED,
            payment_method="ADMIN_MANUAL",
            collected_by_type="ADMIN",
            collected_by_user_id=current_admin.id,
            collected_by_label=f"Admin: {current_admin.full_name}",
        ))
        await db.flush()
        booking = await recompute_booking_ledger(booking.id, db)

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

    # Immediately invalidate L1+L2 cache so availability is reflected
    from app.utils.cache import clear_cache_prefix
    if request.target_type == 'package':
        clear_cache_prefix("packages:list:")
        clear_cache_prefix("packages:detail:")
    elif request.target_type == 'room':
        clear_cache_prefix("rooms:list:")
        clear_cache_prefix("rooms:detail:")

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
        await arq_pool.enqueue_job("process_post_booking_documents_task", booking.id, booking.status == "FULLY_PAID" or booking.status == "CONFIRMED")
        return {
            "status": "success",
            "message": f"Post-booking documents task successfully queued for booking {booking.public_id}."
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to enqueue post-booking documents task: {str(e)}"
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
        await arq_pool.enqueue_job("process_post_booking_documents_task", booking.id, booking.status == "FULLY_PAID" or booking.status == "CONFIRMED")
        return {
            "status": "success",
            "message": f"Post-booking documents task successfully queued for booking {booking.public_id}."
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to enqueue post-booking documents task: {str(e)}"
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

    booking_stmt = select(Booking).where(Booking.id == id).with_for_update()
    booking_res = await db.execute(booking_stmt)
    booking = booking_res.scalar_one_or_none()
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

    sse_payload = None

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
            
            await db.flush()
            import time
            from app.core.timezone import get_ist_now
            from app.models.package import PackageVariant
            v_res = await db.execute(select(PackageVariant).where(PackageVariant.id == booking.variant_id))
            variant = v_res.scalar_one_or_none()
            if variant:
                from app.api.v1.public_packages import get_effective_package_prices
                eff_adult, eff_child = get_effective_package_prices(variant.adult_price, variant.child_price, inv.price_override)
                sse_payload = {
                    "version": int(time.time() * 1000),
                    "timestamp": get_ist_now().isoformat(),
                    "package_id": variant.package_id,
                    "travel_date": str(booking.travel_date),
                    "available": inv.total_capacity - (inv.booked_count + inv.reserved_count),
                    "reserved": inv.reserved_count,
                    "booked": inv.booked_count,
                    "is_closed": inv.is_closed,
                    "effective_adult_price": float(eff_adult),
                    "effective_child_price": float(eff_child),
                    "variant_id": booking.variant_id
                }

    elif booking.room_variant_id:
        # 1. Fetch stay dates
        from app.models.booking import BookingStayDate
        dates_res = await db.execute(
            select(BookingStayDate.date).where(BookingStayDate.booking_id == booking.id)
        )
        stay_dates = [row[0] for row in dates_res.all()]
        
        # 2. Fetch room variant and default times
        from app.models.room import RoomVariant, Room
        rv_res = await db.execute(
            select(RoomVariant, Room.slot_start, Room.slot_end)
            .join(Room, Room.id == RoomVariant.room_id)
            .where(RoomVariant.id == booking.room_variant_id)
        )
        rv_row = rv_res.first()
        if rv_row:
            rv, default_slot_start, default_slot_end = rv_row
            
            pricing = booking.pricing_snapshot or {}
            slot_start_str = pricing.get("slot_start")
            slot_end_str = pricing.get("slot_end")
            
            slot_start = time.fromisoformat(slot_start_str) if slot_start_str else default_slot_start
            slot_end = time.fromisoformat(slot_end_str) if slot_end_str else default_slot_end
            
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
                    logger.info(f"Released {required_rooms} rooms for room variant inventory {booking.room_variant_id} on {stay_date} for slot {slot_start} -> {slot_end}")

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
    
    # Immediately invalidate L1+L2 cache so freed seats are reflected to all visitors
    from app.utils.cache import clear_cache_prefix
    if booking.variant_id:
        clear_cache_prefix("packages:list:")
        clear_cache_prefix("packages:detail:")
    elif booking.room_variant_id:
        clear_cache_prefix("rooms:list:")
        clear_cache_prefix("rooms:detail:")

    if sse_payload:
        from app.utils.sse import sse_manager
        await sse_manager.broadcast_event("package", str(sse_payload["package_id"]), "INVENTORY_UPDATE", sse_payload)

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



class RecordCashPaymentRequest(BaseModel):
    amount: Optional[float] = Field(None, description="Amount to record. Defaults to full remaining balance.")
    payment_method: str = Field("CASH", description="CASH or BANK_TRANSFER")
    collected_by_label: Optional[str] = Field(None, description="Human-readable label, e.g. 'Admin: Ravi'")


@router.post("/{id}/mark-balance-paid")
async def admin_mark_balance_paid_legacy(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """
    Legacy alias kept for backward compatibility with existing frontend calls.
    Delegates to the full record-cash-payment implementation.
    Records the complete remaining balance as CASH collected by admin.
    """
    return await _do_record_cash_payment(
        booking_id=id, amount=None, payment_method="CASH",
        collected_by_label=None, db=db, current_admin=current_admin
    )


@router.post("/{id}/record-cash-payment")
async def admin_record_cash_payment(
    id: int,
    body: RecordCashPaymentRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """
    Admin records a manual cash or bank-transfer payment for a booking.
    Each call appends a new immutable ledger row.
    Idempotent by payment_reference_id (UUID-based).
    Booking status is recomputed from the full payment ledger after each entry.
    """
    return await _do_record_cash_payment(
        booking_id=id, amount=body.amount, payment_method=body.payment_method,
        collected_by_label=body.collected_by_label, db=db, current_admin=current_admin
    )


async def _do_record_cash_payment(
    booking_id: int,
    amount: Optional[float],
    payment_method: str,
    collected_by_label: Optional[str],
    db: AsyncSession,
    current_admin: User,
):
    from app.models.enums import BookingStatus, PaymentStatus
    from app.models.payment import Payment
    from app.utils.ledger import recompute_booking_ledger
    from loguru import logger
    import uuid

    # 1. Fetch booking
    booking_stmt = select(Booking).where(Booking.id == booking_id)
    booking_res = await db.execute(booking_stmt)
    booking = booking_res.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status in (BookingStatus.FULLY_PAID, BookingStatus.CANCELLED, BookingStatus.REFUNDED):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot record payment for a booking with status: {booking.status.value}"
        )

    if booking.remaining_balance <= Decimal("0.01"):
        raise HTTPException(status_code=400, detail="No remaining balance to record payment for")

    # 2. Determine amount to record
    record_amount = Decimal(str(amount)) if amount else booking.remaining_balance
    if record_amount > booking.remaining_balance + Decimal("0.01"):
        raise HTTPException(
            status_code=400,
            detail=f"Amount {record_amount} exceeds remaining balance {booking.remaining_balance}"
        )
    record_amount = min(record_amount, booking.remaining_balance)

    # 3. Generate deterministic idempotency reference
    payment_reference_id = f"CASH_{booking.public_id}_{uuid.uuid4().hex[:8].upper()}"

    # 4. Idempotency guard: check if same reference was already inserted (shouldn't happen with UUID, but guard anyway)
    existing = await db.execute(
        select(Payment).where(Payment.payment_reference_id == payment_reference_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="This payment reference already exists. Duplicate prevented.")

    # 5. Build human-readable label
    label = collected_by_label or f"Admin: {current_admin.full_name}"

    # 6. Insert CAPTURED ledger row directly (cash payments are immediately captured)
    manual_payment = Payment(
        booking_id=booking.id,
        payment_reference_id=payment_reference_id,
        razorpay_order_id=None,
        razorpay_payment_id=None,
        amount=record_amount,
        status=PaymentStatus.CAPTURED,
        payment_method=payment_method.upper(),
        collected_by_type="ADMIN",
        collected_by_user_id=current_admin.id,
        collected_by_label=label,
    )
    db.add(manual_payment)
    await db.flush()

    # 7. Recompute booking status from full ledger — single source of truth
    booking = await recompute_booking_ledger(booking.id, db)

    await db.commit()

    # 8. Queue PDF generation tasks if now fully paid
    try:
        from app.worker import get_arq_pool
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job(
            "process_post_booking_documents_task",
            booking.id,
            booking.status == BookingStatus.FULLY_PAID
        )
    except Exception as arq_err:
        from loguru import logger
        logger.warning(f"Failed to enqueue PDF tasks for admin cash payment: {arq_err}")

    return {
        "status": "success",
        "message": f"Payment of ₹{float(record_amount):,.2f} recorded for booking {booking.public_id}.",
        "booking_status": booking.status.value,
        "paid_amount": float(booking.paid_amount),
        "remaining_balance": float(booking.remaining_balance),
        "payment_reference_id": payment_reference_id,
    }
