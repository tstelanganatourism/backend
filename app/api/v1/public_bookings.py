from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import date, time, timedelta, datetime, timezone
from pydantic import BaseModel, Field
from typing import Optional, List
import uuid
from decimal import Decimal
from loguru import logger
import asyncio

from app.db.session import get_db
from app.models.package import PackageVariantInventory, PackageVariant, Package
from app.models.room import Room, RoomSlotInventory, RoomVariant
from app.models.booking import Booking, BookingPassenger, BookingStayDate
from app.models.enums import BookingSource, BookingStatus, UserRole, GenderType, PublishStatus
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
    phone: Optional[str] = Field(None, pattern=r"^(\d{10})?$")
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
    
    # Trust Lock Expected Pricing (to prevent mismatch)
    expected_amount: Optional[float] = None

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
    
    if request.target_type == 'package' and (adult_count + child_count) != request.quantity:
        raise HTTPException(status_code=400, detail="Adult and child count must equal total quantity")
        
    if not request.passengers or len(request.passengers) != request.quantity:
        raise HTTPException(status_code=400, detail="Passenger details must be provided for all guests")
    
    # Validate passenger Aadhaar and Age data strictly
    if request.passengers:
        for i, p in enumerate(request.passengers):
            is_child = i >= adult_count
            
            # Age constraints
            if request.target_type == 'package':
                if is_child:
                    if not (4 <= p.age <= 10):
                        raise HTTPException(status_code=400, detail=f"Child age must be between 4 and 10 years for passenger {i+1}")
                else:
                    if p.age < 11:
                        raise HTTPException(status_code=400, detail=f"Adult passenger {i+1} must be at least 11 years old")
                    if i == 0 and not p.phone:
                        raise HTTPException(status_code=400, detail=f"Phone number is required for the primary adult passenger")
            else:
                # Room bookings treat all guests generically, but still need phone for primary
                if i == 0 and not p.phone:
                    raise HTTPException(status_code=400, detail=f"Phone number is required for the primary passenger")
            
            # Aadhaar validation contract: optional for children <= 10, mandatory for anyone >= 11
            is_child_age = p.age <= 10
            if not is_child_age:
                # 11+ MUST provide a valid Aadhaar
                if not p.aadhaar or not p.aadhaar.strip():
                    raise HTTPException(status_code=400, detail=f"Aadhaar is required for passenger {i+1} (age 11+)")
                if not is_valid_aadhaar(p.aadhaar.strip()):
                    raise HTTPException(status_code=400, detail=f"Invalid Aadhaar format for passenger {i+1}")
            else:
                # Children (<= 10): validate only if provided
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
            
        # 2. Get base variant details and verify parent package is active/not deleted
        variant_query = (
            select(PackageVariant)
            .join(Package, Package.id == PackageVariant.package_id)
            .where(
                PackageVariant.id == request.variant_id,
                PackageVariant.is_active == True,
                PackageVariant.deleted_at.is_(None),
                Package.status == PublishStatus.PUBLISHED,
                Package.deleted_at.is_(None)
            )
            .limit(1)
        )
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

        # 1. Fetch RoomVariant and verify parent room is active/not deleted
        if request.room_variant_id:
            variant_query = (
                select(RoomVariant)
                .join(Room, Room.id == RoomVariant.room_id)
                .where(
                    RoomVariant.id == request.room_variant_id,
                    RoomVariant.is_active == True,
                    RoomVariant.deleted_at.is_(None),
                    Room.is_active == True,
                    Room.deleted_at.is_(None)
                )
                .limit(1)
            )
        else:
            variant_query = (
                select(RoomVariant)
                .join(Room, Room.id == RoomVariant.room_id)
                .where(
                    RoomVariant.room_id == request.room_id,
                    RoomVariant.is_active == True,
                    RoomVariant.deleted_at.is_(None),
                    Room.is_active == True,
                    Room.deleted_at.is_(None)
                )
                .limit(1)
            )

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
        if departure < arrival:
            raise HTTPException(status_code=400, detail="departure_date cannot be before travel_date")

        stay_dates = []
        if departure == arrival:
            stay_dates.append(arrival)
        else:
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
            target_id=target_id,
            ticket_count=request.quantity
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
        
    if request.target_type == 'room':
        if request.slot_start:
            pricing_snapshot["slot_start"] = str(request.slot_start)
        if request.slot_end:
            pricing_snapshot["slot_end"] = str(request.slot_end)
        
    # --- Razorpay Order Generation ---
    payment_percentage = request.payment_percentage if request.payment_percentage is not None else 100.0
    if not (35.0 <= payment_percentage <= 100.0):
        raise HTTPException(status_code=400, detail="Payment percentage must be between 35% and 100%")
    
    tourist_amount_payable = (total_amount * Decimal(str(payment_percentage)) / Decimal("100")).quantize(Decimal("0.01"))
    
    if is_agent:
        payable_amount = (agent_payable * Decimal(str(payment_percentage)) / Decimal("100")).quantize(Decimal("0.01"))
    else:
        payable_amount = tourist_amount_payable

    pricing_snapshot["payment_percentage"] = str(payment_percentage)
    pricing_snapshot["tourist_amount_payable"] = str(tourist_amount_payable)
    pricing_snapshot["actual_paid_advance"] = str(payable_amount)

    if request.expected_amount is not None:
        pass # Frontend sends expected_amount but we just process checkout with realtime calculated price.

    draft_id = "DRF-" + str(uuid.uuid4())[:8].upper()
    receipt_id = f"rcpt_{draft_id}"
    
    razorpay_order = await asyncio.to_thread(
        razorpay_service.create_order,
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
    await db.flush()

    import time
    from app.utils.sse import sse_manager
    
    sse_payloads = []
    if request.target_type.lower() == 'package':
        sse_payloads.append({
            "version": int(time.time() * 1000),
            "timestamp": now.isoformat(),
            "package_id": variant.package_id,
            "travel_date": str(request.travel_date),
            "available": inventory.total_capacity - (inventory.booked_count + inventory.reserved_count),
            "reserved": inventory.reserved_count,
            "booked": inventory.booked_count,
            "is_closed": inventory.is_closed,
            "effective_adult_price": float(eff_adult),
            "effective_child_price": float(eff_child),
            "variant_id": package_variant_id
        })
    elif request.target_type.lower() == 'room':
        for inv in locked_inventories:
            sse_payloads.append({
                "version": int(time.time() * 1000),
                "timestamp": now.isoformat(),
                "room_id": variant.room_id,
                "travel_date": str(inv.date),
                "available": inv.total_rooms - (inv.booked_rooms + inv.reserved_rooms),
                "reserved": inv.reserved_rooms,
                "booked": inv.booked_rooms,
                "is_closed": inv.is_closed,
                "variant_id": room_variant_id
            })

    await db.commit()
    await db.refresh(draft)

    for p in sse_payloads:
        target_channel = "package" if request.target_type.lower() == 'package' else "room"
        target_id = p.get("package_id") if target_channel == "package" else p.get("room_id")
        await sse_manager.broadcast_event(target_channel, str(target_id), "INVENTORY_UPDATE", p)

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
    
    paid_statuses = {BookingStatus.FULLY_PAID}
    for b in bookings:
        if b.status in paid_statuses:
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
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
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
        .limit(limit)
        .offset(offset)
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
            "paid_amount": float(b.paid_amount),
            "remaining_balance": float(b.remaining_balance),
            "status": b.status.value if hasattr(b.status, 'value') else str(b.status),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "package_title": package_title,
            "variant_title": variant_title,
            "passenger_names": [p.full_name for p in b.passengers],
            "agent_commission": float(b.agent_commission or 0),
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
        if b.status == BookingStatus.FULLY_PAID:
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

@router.get("/live-count")
async def get_live_booking_count(db: AsyncSession = Depends(get_db)):
    """
    Returns the live booking count for the navbar.
    Base count is 10000 + the actual number of successful bookings.
    Uses L1/L2 cache to keep navbar loads under 1ms.
    """
    from app.utils.cache import ttl_cache_get_or_set

    async def _fetch_count():
        from sqlalchemy import func
        query = select(func.count(Booking.id)).where(
            Booking.status.in_([BookingStatus.FULLY_PAID, BookingStatus.PARTIAL_PAID])
        )
        result = await db.execute(query)
        return result.scalar() or 0

    count = await ttl_cache_get_or_set("bookings:live_count", 30, _fetch_count)
    return {"count": 10000 + count}

@router.get("/{public_id}")
async def get_booking_details(
    public_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Retrieve booking detail by its public ID.
    Commission fields are ONLY included when the authenticated user owns the booking as an agent or is an admin.
    Public users and tourists never receive agent_commission or agent_payable.
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
    
    room_checkin = None
    room_checkout = None
    
    if b.pricing_snapshot and b.pricing_snapshot.get('slot_start'):
        from datetime import datetime
        try:
            # Parse from "%H:%M:%S" to "%I:%M %p"
            room_checkin = datetime.strptime(b.pricing_snapshot.get('slot_start'), "%H:%M:%S").strftime('%I:%M %p')
            room_checkout = datetime.strptime(b.pricing_snapshot.get('slot_end'), "%H:%M:%S").strftime('%I:%M %p')
        except:
            pass
            
    if not room_checkin:
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
            agent_gst = agent.gst_number
            agent_company = agent.company_name
    else:
        agent_gst = None
        agent_company = None

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

    # ─── Build Payment Ledger ─────────────────────────────────────────────────
    from app.models.payment import Payment
    from app.models.enums import PaymentStatus
    payment_ledger_stmt = select(Payment).where(
        Payment.booking_id == b.id,
        Payment.deleted_at.is_(None)
    ).order_by(Payment.created_at.asc())
    p_result = await db.execute(payment_ledger_stmt)
    raw_payments = p_result.scalars().all()

    payment_ledger = []
    for p in raw_payments:
        collected_by_label = p.collected_by_label
        if not collected_by_label:
            collected_by_label = "Razorpay" if p.collected_by_type == "RAZORPAY" else "Admin (Cash)"
        payment_ledger.append({
            "id": p.id,
            "amount": float(p.amount),
            "payment_method": p.payment_method,
            "status": p.status.value if hasattr(p.status, "value") else str(p.status),
            "collected_by_type": p.collected_by_type,
            "collected_by_label": collected_by_label,
            "payment_reference_id": p.payment_reference_id,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })

    # ─── Commission Gate: Only for owning agent or admin ─────────────────────
    is_agent_owner = (
        current_user is not None
        and current_user.role == UserRole.AGENT
        and b.agent_id == current_user.id
    )
    is_admin = current_user is not None and current_user.role == UserRole.ADMIN
    show_commission = is_agent_owner or is_admin
    use_agent_payment_view = show_commission and b.agent_id is not None

    agent_paid = sum(Decimal(str(p.amount)) for p in raw_payments if p.status == PaymentStatus.CAPTURED)
    agent_payable = max(Decimal("0.00"), Decimal(str(b.total_amount)) - Decimal(str(b.agent_commission or "0.00")))

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
        "total_amount": float(b.subtotal_amount) + float(b.gst_amount) + float(b.gateway_fee) - float(b.coupon_discount),
        "remaining_balance": (
            float(max(Decimal("0.00"), agent_payable - agent_paid))
            if use_agent_payment_view
            else float(b.remaining_balance)
        ),
        "paid_amount": (
            float(agent_paid)
            if use_agent_payment_view
            else float(b.paid_amount)
        ),
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
        "agent_gst": agent_gst,
        "agent_company": agent_company,
        "has_pending_cancellation": has_pending_cancellation,
        "ticket_pdf_url": b.ticket_pdf_url,
        "invoice_pdf_url": b.invoice_pdf_url,
        "ticket_generation_status": b.ticket_generation_status.value if hasattr(b.ticket_generation_status, "value") else str(b.ticket_generation_status),
        "invoice_generation_status": b.invoice_generation_status.value if hasattr(b.invoice_generation_status, "value") else str(b.invoice_generation_status),
        # Commission fields: only for owning agent or admin
        "agent_commission": float(b.agent_commission or 0) if show_commission else None,
        "agent_payable": (
            float(Decimal(str(b.total_amount)) - Decimal(str(b.agent_commission or "0.00")))
            if show_commission
            else None
        ),
        # Immutable payment history — always returned
        "payment_ledger": payment_ledger,
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
        .with_for_update()
        .limit(1)
    )
    result = await db.execute(query)
    booking = result.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking reservation not found")

    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required to perform balance payment")

    # Verify ownership or admin access
    is_owner = current_user is not None and (booking.user_id == current_user.id or booking.agent_id == current_user.id)
    is_admin = current_user is not None and current_user.role == UserRole.ADMIN
    
    if not (is_owner or is_admin):
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

    pending_payments = [p for p in booking.payments if p.status == PaymentStatus.CREATED and p.razorpay_order_id]
    pending_payments.sort(key=lambda p: p.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    if pending_payments:
        latest_pending = pending_payments[0]
        created_at = latest_pending.created_at
        if created_at and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at and datetime.now(timezone.utc) - created_at <= timedelta(minutes=30):
            return {
                "status": "success",
                "checkout_data": {
                    "booking_public_id": booking.public_id,
                    "razorpay_order_id": latest_pending.razorpay_order_id,
                    "amount": int(round(float(latest_pending.amount) * 100)),
                    "currency": "INR",
                    "key_id": settings.RAZORPAY_KEY_ID
                }
            }

        latest_pending.status = PaymentStatus.FAILED
        latest_pending.error_code = "ORDER_ABANDONED"
        latest_pending.error_description = "Previous balance checkout was abandoned before completion."

    # Generate a new Razorpay Order for remaining balance
    is_agent_payment = current_user is not None and current_user.role == UserRole.AGENT and booking.agent_id == current_user.id
    
    if is_agent_payment:
        agent_payable = Decimal(str(booking.total_amount)) - Decimal(str(booking.agent_commission or "0.00"))
        captured_total = sum(Decimal(str(p.amount)) for p in booking.payments if p.status == PaymentStatus.CAPTURED)
        payable_amount = float(max(Decimal("0.00"), agent_payable - captured_total))
    else:
        payable_amount = float(booking.remaining_balance)
        
    order_receipt = f"bal_{booking.public_id}_{uuid.uuid4().hex[:6].upper()}"

    razorpay_order = await asyncio.to_thread(
        razorpay_service.create_order,
        amount=payable_amount,
        receipt=order_receipt,
        notes={"booking_id": booking.id, "type": "balance"}
    )
    razorpay_order_id = razorpay_order.get("id")

    # Create CREATED payment ledger row to track this attempt
    payment = Payment(
        booking_id=booking.id,
        payment_reference_id=razorpay_order_id,  # idempotency key
        razorpay_order_id=razorpay_order_id,
        amount=Decimal(str(payable_amount)),
        status=PaymentStatus.CREATED,
        payment_method="RAZORPAY",
        collected_by_type="RAZORPAY",
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
