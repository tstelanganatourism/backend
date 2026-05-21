from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date, time, timedelta
from pydantic import BaseModel, Field
from typing import Optional, List
import uuid
from decimal import Decimal
from loguru import logger

from app.db.session import get_db
from app.models.package import PackageVariantInventory, PackageVariant, Package
from app.models.room import RoomSlotInventory, RoomVariant
from app.models.booking import Booking, BookingPassenger, BookingStayDate
from app.models.enums import BookingSource, BookingStatus, UserRole, GenderType
from app.models.user import User
from app.models.coupon import Coupon
from app.middleware.auth import get_current_user_optional, require_agent, get_current_user
from app.core.security import AadharCryptography, AadharHashing
from sqlalchemy.orm import selectinload
from app.utils.pricing import get_effective_package_prices

router = APIRouter()

# ─── Aadhaar Masking Utility ─────────────────────────────────────────────────

def _mask_aadhaar(encrypted_value: str) -> str:
    """Decrypt an Aadhaar and return masked format XXXX-XXXX-1234."""
    try:
        crypto = AadharCryptography()
        raw = crypto.decrypt(encrypted_value)
        last4 = raw[-4:] if len(raw) >= 4 else raw
        return f"XXXX-XXXX-{last4}"
    except Exception:
        return "XXXX-XXXX-****"

# ─── Passenger Input Schema ───────────────────────────────────────────────────

class PassengerInput(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=200)
    age: int = Field(..., ge=0, le=150)
    gender: Optional[str] = None  # MALE / FEMALE / OTHER
    phone: Optional[str] = Field(None, pattern=r"^\d{10}$")
    aadhaar: Optional[str] = Field(None, max_length=20)  # Required for adults, optional for children (<10)
    relationship: Optional[str] = None  # e.g. 'self', 'spouse', 'child'
    is_primary: Optional[bool] = False

class CheckoutRequest(BaseModel):
    # Differentiate between package or room
    target_type: str  # 'package' or 'room'

    # Common
    travel_date: date
    quantity: int  # Total passengers / guests

    # Package specific
    variant_id: Optional[int] = None

    # Room specific
    room_id: Optional[int] = None
    room_variant_id: Optional[int] = None
    slot_start: Optional[time] = None
    slot_end: Optional[time] = None
    departure_date: Optional[date] = None  # Multi-day room support

    # Custom Checkout fields for precise billing
    coupon_code: Optional[str] = None
    adult_count: Optional[int] = None
    child_count: Optional[int] = None
    has_refreshment_addon: Optional[bool] = False

    # Passenger manifest
    passengers: Optional[List[PassengerInput]] = None
    
    # Partial payment option (>=35% and <100%)
    payment_percentage: Optional[float] = 100.0

@router.post("/checkout")
async def process_checkout(
    request: CheckoutRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Checkout API (Draft-First Architecture). 
    Validates inventory (locks via reserved_count), applies pricing logic,
    generates a Razorpay Order, and creates a temporary BookingDraft.
    """
    from app.services.razorpay_client import razorpay_service
    from app.models.booking import BookingDraft
    from app.core.timezone import get_ist_now
    from app.utils.verhoeff import is_valid_aadhaar
    
    # Derive adult and child count safely
    adult_count = request.adult_count if request.adult_count is not None else request.quantity
    child_count = request.child_count if request.child_count is not None else 0
    
    # Validate passenger Aadhaar and Age data strictly
    if request.passengers:
        for i, p in enumerate(request.passengers):
            is_child = i >= adult_count
            
            # Age constraints
            if is_child:
                if not (4 <= p.age <= 10):
                    raise HTTPException(status_code=400, detail=f"Child age must be between 4 and 10 years for passenger {i+1}")
            else:
                if p.age < 11:
                    raise HTTPException(status_code=400, detail=f"Adult passenger {i+1} must be at least 11 years old")
                if not p.phone:
                    raise HTTPException(status_code=400, detail=f"Phone number is required for adult passenger {i+1}")
            
            # Aadhaar validation contract: optional for minors under 18, mandatory for adults 18+
            is_minor = p.age < 18
            if not is_minor:
                # Adults MUST provide a valid Aadhaar
                if not p.aadhaar or not p.aadhaar.strip():
                    raise HTTPException(status_code=400, detail=f"Aadhaar is required for adult passenger {i+1}")
                if not is_valid_aadhaar(p.aadhaar.strip()):
                    raise HTTPException(status_code=400, detail=f"Invalid Aadhaar format for passenger {i+1}")
            else:
                # Minors: validate only if provided
                if p.aadhaar and p.aadhaar.strip() and not is_valid_aadhaar(p.aadhaar.strip()):
                    raise HTTPException(status_code=400, detail=f"Invalid Aadhaar format for passenger {i+1}")
    
    has_refreshment_addon = request.has_refreshment_addon or False
    
    subtotal_amount = Decimal("0.00")
    room_variant_id = None
    package_variant_id = None
    _room_stay_dates = []  # Populated for room bookings with multi-day stays
    
    # Start inventory validation scope under SELECT FOR UPDATE
    if request.target_type == 'package':
        if not request.variant_id:
            raise HTTPException(status_code=400, detail="variant_id is required for package")
            
        package_variant_id = request.variant_id
        
        # 1. Fetch inventory row with SELECT FOR UPDATE
        inventory_query = (
            select(PackageVariantInventory)
            .where(
                PackageVariantInventory.variant_id == request.variant_id,
                PackageVariantInventory.date == request.travel_date
            )
            .with_for_update()
        )
        
        result = await db.execute(inventory_query)
        inventory = result.scalar_one_or_none()
        
        if not inventory:
            raise HTTPException(status_code=404, detail="Inventory not found for this date")
            
        if inventory.is_closed:
            raise HTTPException(status_code=400, detail="Booking closed for this date")
            
        available = inventory.total_capacity - (inventory.booked_count + inventory.reserved_count)
        if available < request.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient inventory. Requested: {request.quantity}, Available: {available}")
            
        # 2. Get base variant details
        variant_query = select(PackageVariant).where(PackageVariant.id == request.variant_id).limit(1)
        v_res = await db.execute(variant_query)
        variant = v_res.scalar_one_or_none()
        if not variant:
            raise HTTPException(status_code=400, detail="Package variant not found")
            
        # Calculate subtotal using precise Decimal representation
        eff_adult, eff_child = get_effective_package_prices(
            variant.adult_price, variant.child_price, inventory.price_override
        )
        subtotal_amount = Decimal(str(adult_count)) * eff_adult + Decimal(str(child_count)) * eff_child
        
        # Reserve inventory (increment reserved_count instead of booked_count)
        inventory.reserved_count += request.quantity
        
    elif request.target_type == 'room':
        if not request.room_variant_id and not request.room_id:
            raise HTTPException(status_code=400, detail="room_variant_id or room_id is required for room")
        if not request.slot_start or not request.slot_end:
            raise HTTPException(status_code=400, detail="slot_start and slot_end are required for room")

        # 1. Fetch RoomVariant
        if request.room_variant_id:
            variant_query = select(RoomVariant).where(
                RoomVariant.id == request.room_variant_id,
                RoomVariant.deleted_at.is_(None)
            ).limit(1)
        else:
            variant_query = select(RoomVariant).where(
                RoomVariant.room_id == request.room_id,
                RoomVariant.deleted_at.is_(None)
            ).limit(1)

        v_res = await db.execute(variant_query)
        variant = v_res.scalar_one_or_none()

        if not variant:
            raise HTTPException(
                status_code=400,
                detail="Active Room variant not found to determine capacity"
            )

        room_variant_id = variant.id

        # 2. Calculate required rooms
        from app.services.room_calculation import calculate_required_rooms
        required_rooms = calculate_required_rooms(request.quantity, variant.capacity_per_room)

        # 3. Generate stay date range (arrival inclusive, departure exclusive)
        arrival = request.travel_date
        departure = request.departure_date or (arrival + timedelta(days=1))
        if departure <= arrival:
            raise HTTPException(status_code=400, detail="departure_date must be after travel_date")

        stay_dates = []
        current = arrival
        while current < departure:
            stay_dates.append(current)
            current += timedelta(days=1)

        # 4. Lock ALL inventory slots across the entire stay with SELECT FOR UPDATE
        locked_inventories = []
        for stay_date in stay_dates:
            inv_query = (
                select(RoomSlotInventory)
                .where(
                    RoomSlotInventory.room_variant_id == room_variant_id,
                    RoomSlotInventory.date == stay_date,
                    RoomSlotInventory.slot_start == request.slot_start,
                    RoomSlotInventory.slot_end == request.slot_end
                )
                .with_for_update()
            )
            inv_result = await db.execute(inv_query)
            inv = inv_result.scalar_one_or_none()

            if not inv:
                raise HTTPException(
                    status_code=400,
                    detail=f"Rooms unavailable on {stay_date.isoformat()}"
                )

            if inv.is_closed:
                raise HTTPException(
                    status_code=400,
                    detail=f"Rooms unavailable on {stay_date.isoformat()}"
                )

            available = inv.total_rooms - (inv.booked_rooms + inv.reserved_rooms)
            if available < required_rooms:
                raise HTTPException(
                    status_code=400,
                    detail=f"Rooms unavailable on {stay_date.isoformat()}"
                )
            locked_inventories.append(inv)

        # 5. All dates validated — reserve inventory and calculate pricing
        subtotal_amount = Decimal("0.00")
        for i, inv in enumerate(locked_inventories):
            inv.reserved_rooms += required_rooms
            day = stay_dates[i]
            is_weekend = day.weekday() in (5, 6)
            special_price = getattr(inv, 'special_day_price', None)
            if special_price is not None:
                day_price = Decimal(str(special_price))
            elif is_weekend:
                day_price = variant.weekend_price
            else:
                day_price = variant.weekday_price
            subtotal_amount += Decimal(str(required_rooms)) * day_price

        # Store stay dates for persistence
        _room_stay_dates = stay_dates
    else:
        raise HTTPException(status_code=400, detail="Invalid target_type")
        
    # --- Billing & Coupon Validation Math ---
    coupon_discount = Decimal("0.00")
    coupon_applied = None
    
    if request.coupon_code:
        coupon_query = (
            select(Coupon)
            .where(
                Coupon.code == request.coupon_code.upper(),
                Coupon.is_active == True,
                Coupon.deleted_at.is_(None)
            )
            .with_for_update()
        )
        c_res = await db.execute(coupon_query)
        coupon = c_res.scalar_one_or_none()
        
        # Determine the target_id based on the type
        target_id = None
        if request.target_type.lower() == 'package':
            target_id = variant.package_id
        elif request.target_type.lower() == 'room':
            target_id = variant.room_id
            
        if not coupon or not coupon.is_valid(
            booking_amount=float(subtotal_amount),
            target_type=request.target_type.upper(),
            target_id=target_id
        ):
            raise HTTPException(status_code=400, detail="Invalid or expired promo code.")
            
        coupon_discount = Decimal(str(coupon.calculate_discount(float(subtotal_amount))))
        coupon_applied = coupon.code
        # IMPORTANT: Do not increment usage_count yet! Deferred to webhook confirmation.
        
    discounted_subtotal = max(Decimal("0.00"), subtotal_amount - coupon_discount)
    gst_amount = (discounted_subtotal * Decimal("0.05")).quantize(Decimal("0.01"))
    gateway_fee = ((discounted_subtotal + gst_amount) * Decimal("0.01")).quantize(Decimal("0.01"))
    total_amount = discounted_subtotal + gst_amount + gateway_fee
    
    # --- Agent Metadata & Commission Calculations ---
    is_agent = current_user is not None and current_user.role == UserRole.AGENT
    
    agent_id = None
    agent_name = None
    commission_percentage = Decimal("0.00")
    agent_discount = Decimal("0.00")
    agent_payable = total_amount
    source = BookingSource.PUBLIC
    
    if is_agent:
        agent_id = current_user.id
        agent_name = current_user.full_name
        source = BookingSource.AGENT
        commission_percentage = Decimal(str(current_user.commission_percentage or 0))
        
        commission_type = getattr(current_user, 'commission_type', 'PERCENTAGE') or 'PERCENTAGE'
        
        if commission_type == 'FIXED_AMOUNT':
            fixed_amount = Decimal(str(current_user.commission_fixed_amount or 0))
            agent_discount = min(fixed_amount, total_amount).quantize(Decimal("0.01"))
        else:
            # PERCENTAGE type
            agent_discount = (
                discounted_subtotal
                * commission_percentage
                / Decimal("100")
            ).quantize(Decimal("0.01"))
            # Clamp: never exceed the total
            agent_discount = min(agent_discount, total_amount)
        
        agent_payable = max(Decimal("0.00"), total_amount - agent_discount)
        
    # Construct historical pricing snapshot with Decimal-safe string values
    pricing_snapshot = {
        "subtotal_amount": str(subtotal_amount),
        "coupon_discount": str(coupon_discount),
        "coupon_applied": coupon_applied,
        "gst_amount": str(gst_amount),
        "gateway_fee": str(gateway_fee),
        "tourist_total": str(total_amount),
        "agent_discount": str(agent_discount),
        "agent_payable": str(agent_payable),
    }
    if is_agent:
        pricing_snapshot["agent_metadata"] = {
            "agent_id": current_user.id,
            "agent_name": current_user.full_name,
            "commission_percentage": str(commission_percentage),
        }
        
        
    # --- Razorpay Order Generation ---
    payment_percentage = request.payment_percentage if request.payment_percentage is not None else 100.0
    if not is_agent:
        if not (35.0 <= payment_percentage <= 100.0):
            raise HTTPException(status_code=400, detail="Payment percentage must be between 35% and 100%")
        payable_amount = (total_amount * Decimal(str(payment_percentage)) / Decimal("100")).quantize(Decimal("0.01"))
    else:
        # Agents pay full amount in one shot
        payable_amount = agent_payable
        payment_percentage = 100.0

    pricing_snapshot["payment_percentage"] = str(payment_percentage)
    pricing_snapshot["actual_paid_advance"] = str(payable_amount)

    draft_id = "DRF-" + str(uuid.uuid4())[:8].upper()
    receipt_id = f"rcpt_{draft_id}"
    
    razorpay_order = razorpay_service.create_order(
        amount=float(payable_amount),
        receipt=receipt_id,
        notes={"draft_id": draft_id}
    )
    razorpay_order_id = razorpay_order.get("id")
        
    # --- Database Draft Persistence ---
    now = get_ist_now()
    expires_at = now + timedelta(minutes=10)
    
    # Serialize complete payload to JSON
    # Safe dump of the entire request
    payload_dump = request.model_dump(mode='json')
    
    draft = BookingDraft(
        draft_id=draft_id,
        razorpay_order_id=razorpay_order_id,
        user_id=current_user.id if current_user else None,
        agent_id=agent_id,
        checkout_payload=payload_dump,
        pricing_snapshot=pricing_snapshot,
        target_type=request.target_type,
        variant_id=package_variant_id,
        room_variant_id=room_variant_id,
        travel_date=request.travel_date,
        quantity=request.quantity,
        amount_payable=Decimal(str(payable_amount)),
        coupon_applied=coupon_applied,
        expires_at=expires_at
    )
    db.add(draft)
    await db.commit()
    await db.refresh(draft)

    logger.info(f"BookingDraft {draft_id} created | order={razorpay_order_id} | expires={expires_at.isoformat()}")

    from app.core.config import settings
    return {
        "status": "success",
        "message": "Draft created and inventory reserved.",
        "checkout_data": {
            "draft_id": draft.draft_id,
            "razorpay_order_id": draft.razorpay_order_id,
            "amount": razorpay_order.get("amount"), # in paise
            "currency": "INR",
            "key_id": settings.RAZORPAY_KEY_ID,
            "expires_at": draft.expires_at.isoformat()
        }
    }

@router.get("/agent/dashboard-summary")
async def get_agent_dashboard_summary(
    current_user: User = Depends(require_agent),
    db: AsyncSession = Depends(get_db)
):
    """
    Fetch high-level KPIs for the logged-in agent's sales and earnings dashboard.
    """
    from app.models.booking import BookingPassenger
    from app.core.timezone import get_ist_now
    from sqlalchemy import func
    
    # 1. Fetch bookings
    query = (
        select(Booking)
        .where(
            Booking.agent_id == current_user.id,
            Booking.deleted_at.is_(None)
        )
    )
    result = await db.execute(query)
    bookings = result.scalars().all()
    
    booking_count = len(bookings)
    
    # 2. Earnings Math
    now = get_ist_now()
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    total_earnings = Decimal("0.00")
    this_month_earnings = Decimal("0.00")
    
    for b in bookings:
        if b.status == BookingStatus.CONFIRMED:
            comm = b.agent_commission or Decimal("0.00")
            total_earnings += comm
            if b.created_at and b.created_at >= start_of_month:
                this_month_earnings += comm
                
    # 3. Passenger Count (Total Customers)
    passenger_query = (
        select(func.count(BookingPassenger.id))
        .join(Booking, Booking.id == BookingPassenger.booking_id)
        .where(
            Booking.agent_id == current_user.id,
            Booking.deleted_at.is_(None)
        )
    )
    p_result = await db.execute(passenger_query)
    total_customers = p_result.scalar_one() or 0
            
    return {
        "booking_count": booking_count,
        "total_earnings": float(total_earnings),
        "this_month_earnings": float(this_month_earnings),
        "total_customers": total_customers
    }

@router.get("/agent/bookings")
async def get_agent_bookings(
    current_user: User = Depends(require_agent),
    db: AsyncSession = Depends(get_db)
):
    """
    Fetch recent bookings for the logged-in agent, strictly sanitizing commission details.
    """
    query = (
        select(Booking, Package.title, PackageVariant.title)
        .outerjoin(PackageVariant, Booking.variant_id == PackageVariant.id)
        .outerjoin(Package, PackageVariant.package_id == Package.id)
        .options(selectinload(Booking.passengers))
        .where(
            Booking.agent_id == current_user.id,
            Booking.deleted_at.is_(None)
        )
        .order_by(Booking.created_at.desc())
    )
    result = await db.execute(query)
    rows = result.all()
    
    sanitized_items = []
    for row in rows:
        b = row[0]
        package_title = row[1] or "Custom Lodging / Stays"
        variant_title = row[2] or "Lodge Room Booking"
        
        sanitized_items.append({
            "id": b.id,
            "public_id": b.public_id,
            "travel_date": b.travel_date.isoformat(),
            "adult_count": b.adult_count,
            "child_count": b.child_count,
            "subtotal_amount": float(b.subtotal_amount),
            "coupon_discount": float(b.coupon_discount),
            "coupon_applied": b.coupon_applied,
            "gst_amount": float(b.gst_amount),
            "gateway_fee": float(b.gateway_fee),
            "total_amount": float(b.total_amount),
            "remaining_balance": float(b.remaining_balance),
            "status": b.status.value if hasattr(b.status, 'value') else str(b.status),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "package_title": package_title,
            "variant_title": variant_title,
            "passenger_names": [p.full_name for p in b.passengers],
        })
        
    return sanitized_items

@router.get("/user/dashboard-summary")
async def get_tourist_dashboard_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Fetch high-level KPIs for the logged-in tourist's dashboard.
    """
    query = (
        select(Booking)
        .where(
            Booking.user_id == current_user.id,
            Booking.deleted_at.is_(None),
            Booking.status != BookingStatus.PENDING
        )
    )
    result = await db.execute(query)
    bookings = result.scalars().all()
    
    booking_count = len(bookings)
    
    past_trips = 0
    upcoming_trips = 0
    from datetime import date
    today = date.today()
    for b in bookings:
        if b.status == BookingStatus.CONFIRMED or b.status == BookingStatus.FULLY_PAID:
            if b.travel_date < today:
                past_trips += 1
            else:
                upcoming_trips += 1
                
    return {
        "booking_count": booking_count,
        "past_trips": past_trips,
        "upcoming_trips": upcoming_trips
    }

@router.get("/user/bookings")
async def get_tourist_bookings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Fetch recent bookings for the logged-in tourist.
    """
    query = (
        select(Booking, Package.title, PackageVariant.title)
        .outerjoin(PackageVariant, Booking.variant_id == PackageVariant.id)
        .outerjoin(Package, PackageVariant.package_id == Package.id)
        .options(selectinload(Booking.passengers))
        .where(
            Booking.user_id == current_user.id,
            Booking.deleted_at.is_(None),
            Booking.status != BookingStatus.PENDING
        )
        .order_by(Booking.created_at.desc())
    )
    result = await db.execute(query)
    rows = result.all()
    
    sanitized_items = []
    for row in rows:
        b = row[0]
        package_title = row[1] or "Custom Lodging / Stays"
        variant_title = row[2] or "Lodge Room Booking"
        
        sanitized_items.append({
            "id": b.id,
            "public_id": b.public_id,
            "travel_date": b.travel_date.isoformat(),
            "adult_count": b.adult_count,
            "child_count": b.child_count,
            "total_amount": float(b.total_amount),
            "status": b.status.value if hasattr(b.status, 'value') else str(b.status),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "package_title": package_title,
            "variant_title": variant_title,
            "passenger_names": [p.full_name for p in b.passengers],
        })
        
    return sanitized_items

@router.get("/{public_id}")
async def get_booking_details(
    public_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve booking detail by its public ID, securely sanitizing any internal agent commission info.
    """
    from app.models.room import Room, RoomVariant
    query = (
        select(Booking, Package.title, PackageVariant.title, Room.lodge_name, RoomVariant.variant_name, Room.slot_start, Room.slot_end, Room.address)
        .outerjoin(PackageVariant, Booking.variant_id == PackageVariant.id)
        .outerjoin(Package, PackageVariant.package_id == Package.id)
        .outerjoin(RoomVariant, Booking.room_variant_id == RoomVariant.id)
        .outerjoin(Room, RoomVariant.room_id == Room.id)
        .options(selectinload(Booking.passengers))
        .where(
            Booking.public_id == public_id,
            Booking.deleted_at.is_(None)
        )
        .limit(1)
    )
    result = await db.execute(query)
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Booking reservation not found")
        
    b = row[0]
    package_title = row[1] if b.variant_id else row[3]
    variant_title = row[2] if b.variant_id else row[4]
    
    room_checkin = row[5].strftime('%I:%M %p') if row[5] else None
    room_checkout = row[6].strftime('%I:%M %p') if row[6] else None
    room_address = row[7] if row[7] else None
    
    agent_id = None
    agent_name = None
    agent_phone = None
    if b.agent_id:
        agent_query = select(User).where(User.id == b.agent_id).limit(1)
        a_result = await db.execute(agent_query)
        agent = a_result.scalar_one_or_none()
        if agent:
            agent_id = agent.id
            agent_name = agent.full_name
            agent_phone = agent.phone_number

    boarding_point = None
    itinerary = []
    if b.variant_id:
        from app.models.package import PackageBoardingPoint, PackageItineraryDay
        var_stmt = select(PackageVariant).where(PackageVariant.id == b.variant_id)
        var_res = await db.execute(var_stmt)
        variant = var_res.scalar_one_or_none()
        if variant:
            bp_stmt = select(PackageBoardingPoint).where(
                PackageBoardingPoint.package_id == variant.package_id,
                PackageBoardingPoint.deleted_at.is_(None)
            ).order_by(PackageBoardingPoint.sort_order.asc())
            bp_res = await db.execute(bp_stmt)
            boarding_point = bp_res.scalars().first()

            it_stmt = select(PackageItineraryDay).where(
                PackageItineraryDay.package_id == variant.package_id,
                PackageItineraryDay.deleted_at.is_(None)
            ).order_by(PackageItineraryDay.sort_order.asc())
            it_res = await db.execute(it_stmt)
            for day in it_res.scalars().all():
                itinerary.append({
                    "day_number": day.day_number,
                    "title": day.title,
                    "timing": day.timing,
                    "duration": day.duration_at_stop,
                    "meal_included": day.meal_included,
                    "description": day.description
                })
    from app.models.booking import CancellationRequest
    from app.models.enums import CancellationStatus
    cancel_query = select(CancellationRequest).where(
        CancellationRequest.booking_id == b.id,
        CancellationRequest.status == CancellationStatus.PENDING
    ).limit(1)
    cancel_result = await db.execute(cancel_query)
    has_pending_cancellation = cancel_result.scalar_one_or_none() is not None

    return {
        "id": b.id,
        "public_id": b.public_id,
        "target_type": "ROOM" if b.room_variant_id else "PACKAGE",
        "travel_date": b.travel_date.isoformat(),
        "adult_count": b.adult_count,
        "child_count": b.child_count,
        "subtotal_amount": float(b.subtotal_amount),
        "coupon_discount": float(b.coupon_discount),
        "coupon_applied": b.coupon_applied,
        "gst_amount": float(b.gst_amount),
        "gateway_fee": float(b.gateway_fee),
        "total_amount": float(b.total_amount),
        "remaining_balance": float(b.remaining_balance),
        "status": b.status.value if hasattr(b.status, 'value') else str(b.status),
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "package_title": package_title,
        "variant_title": variant_title,
        "boarding_point": {
            "title": boarding_point.title,
            "address": boarding_point.address,
            "landmark": boarding_point.landmark,
            "departure_time": boarding_point.departure_time,
            "contact_number": boarding_point.contact_number
        } if boarding_point else None,
        "room_checkin": room_checkin,
        "room_checkout": room_checkout,
        "room_address": room_address,
        "itinerary": itinerary,
        "passengers": [
            {
                "full_name": p.full_name,
                "age": p.age,
                "gender": p.gender.value if hasattr(p.gender, "value") and p.gender else None,
                "is_child": p.is_child,
                "phone_number": p.phone_number,
                "relationship": p.relationship_to_lead,
                "is_primary": p.is_primary,
                "masked_aadhaar": _mask_aadhaar(p.aadhar_encrypted) if p.aadhar_encrypted else None,
                "id_proof_type": "Aadhaar" if p.aadhar_encrypted else None,
                "id_proof_number": _mask_aadhaar(p.aadhar_encrypted) if p.aadhar_encrypted else None,
            }
            for p in b.passengers
        ],
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agent_phone": agent_phone,
        "has_pending_cancellation": has_pending_cancellation,
        "ticket_pdf_url": b.ticket_pdf_url,
        "invoice_pdf_url": b.invoice_pdf_url,
        "ticket_generation_status": b.ticket_generation_status.value if hasattr(b.ticket_generation_status, "value") else str(b.ticket_generation_status),
        "invoice_generation_status": b.invoice_generation_status.value if hasattr(b.invoice_generation_status, "value") else str(b.invoice_generation_status)
    }

class CancellationRequestInput(BaseModel):
    reason: str = Field(..., min_length=5, max_length=1000)

@router.post("/{public_id}/cancel")
async def request_booking_cancellation(
    public_id: str,
    req: CancellationRequestInput,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """
    Tourist/Agent cancels booking:
    1. Fetch booking by public ID.
    2. Check ownership (must belong to tourist or agent).
    3. Ensure booking is not already CANCELLED or REFUNDED.
    4. Enforce 24-hour limit before travel.
    5. Create CancellationRequest in PENDING state.
    """
    from app.models.booking import Booking, CancellationRequest
    from app.models.enums import CancellationStatus, BookingStatus
    from datetime import datetime, time
    from app.core.timezone import get_ist_now

    query = (
        select(Booking)
        .where(
            Booking.public_id == public_id,
            Booking.deleted_at.is_(None)
        )
        .limit(1)
    )
    result = await db.execute(query)
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking reservation not found")

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required to request cancellation")

    is_owner = (booking.user_id == current_user.id or booking.agent_id == current_user.id)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Not authorized to cancel this booking")

    if booking.status in (BookingStatus.CANCELLED, BookingStatus.REFUNDED):
        raise HTTPException(status_code=400, detail="Booking is already cancelled or refunded")

    # Enforce 7-day cancellation restriction (travel_date > current_date + 7 days)
    today = get_ist_now().date()
    if booking.travel_date <= today + timedelta(days=7):
        raise HTTPException(
            status_code=400,
            detail="Cancellation unavailable within 7 days of travel"
        )

    # Check for pending cancellation requests
    pending_query = select(CancellationRequest).where(
        CancellationRequest.booking_id == booking.id,
        CancellationRequest.status == CancellationStatus.PENDING
    ).limit(1)
    p_result = await db.execute(pending_query)
    existing_request = p_result.scalar_one_or_none()
    if existing_request:
        raise HTTPException(status_code=400, detail="A cancellation request is already pending for this booking")

    cancel_req = CancellationRequest(
        booking_id=booking.id,
        reason=req.reason,
        status=CancellationStatus.PENDING
    )
    db.add(cancel_req)
    await db.commit()

    return {
        "status": "success",
        "message": "Cancellation request submitted successfully and is pending admin review."
    }

@router.post("/{public_id}/balance-checkout")
async def process_balance_checkout(
    public_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Tourist balance checkout.
    Generates a Razorpay Order for the remaining balance.
    """
    from app.services.razorpay_client import razorpay_service
    from app.models.payment import Payment
    from app.models.enums import PaymentStatus
    from app.core.config import settings

    # Fetch booking
    query = (
        select(Booking)
        .options(selectinload(Booking.payments))
        .where(
            Booking.public_id == public_id,
            Booking.deleted_at.is_(None)
        )
        .limit(1)
    )
    result = await db.execute(query)
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking reservation not found")

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required to perform balance payment")

    # Verify ownership
    is_owner = (booking.user_id == current_user.id or booking.agent_id == current_user.id)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Not authorized to pay balance for this booking")

    if booking.status != BookingStatus.PARTIAL_PAID:
        raise HTTPException(status_code=400, detail="Balance payment only allowed for partially paid bookings")

    if booking.remaining_balance <= Decimal("0.01"):
        raise HTTPException(status_code=400, detail="No remaining balance to pay")

    # Enforce maximum payment attempts (1 initial + 1 balance payment, no 3rd payment)
    # Count how many captured payments already exist
    captured_payments_count = sum(1 for p in booking.payments if p.status == PaymentStatus.CAPTURED)
    if captured_payments_count >= 2:
        raise HTTPException(status_code=400, detail="Maximum payment attempts reached for this booking")

    # Generate a new Razorpay Order for remaining balance
    payable_amount = float(booking.remaining_balance)
    order_receipt = f"bal_{booking.public_id}_{uuid.uuid4().hex[:6].upper()}"

    razorpay_order = razorpay_service.create_order(
        amount=payable_amount,
        receipt=order_receipt,
        notes={"booking_id": booking.id, "type": "balance"}
    )
    razorpay_order_id = razorpay_order.get("id")

    # Create CREATED payment record to track this attempt
    payment = Payment(
        booking_id=booking.id,
        razorpay_order_id=razorpay_order_id,
        amount=booking.remaining_balance,
        status=PaymentStatus.CREATED,
        payment_method="RAZORPAY"
    )
    db.add(payment)
    await db.commit()

    return {
        "status": "success",
        "checkout_data": {
            "booking_public_id": booking.public_id,
            "razorpay_order_id": razorpay_order_id,
            "amount": razorpay_order.get("amount"), # in paise
            "currency": "INR",
            "key_id": settings.RAZORPAY_KEY_ID
        }
    }


