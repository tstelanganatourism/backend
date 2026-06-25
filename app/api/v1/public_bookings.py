from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import date, time, timedelta, datetime, timezone
from pydantic import BaseModel, Field
from typing import Optional, List
import uuid
from decimal import Decimal
from loguru import logger
import asyncio

from app.db.session import get_db
from app.models.package import PackageVariantInventory, PackageVariant, Package, PackageBoardingPoint, PackageTransportOption
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
    student_class: Optional[str] = Field(None, max_length=100)  # Class/grade for student packages

class TransportSelection(BaseModel):
    """A single transport option with the quantity of vehicles/passes selected."""
    option_id: int
    quantity: int = Field(..., ge=1)

class CheckoutRequest(BaseModel):
    # Differentiate between package or room
    target_type: str  # 'package' or 'room'

    # Common
    travel_date: date
    quantity: int = Field(..., ge=1)  # Total passengers / guests

    # Package specific
    variant_id: Optional[int] = None
    # Deprecated: use transport_selections instead (kept for backward compat)
    transport_option_id: Optional[int] = None
    # New: supports multiple vehicle types with quantities
    transport_selections: Optional[List[TransportSelection]] = None
    include_refreshments: Optional[bool] = False

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
    student_count: Optional[int] = None  # for student packages
    has_refreshment_addon: Optional[bool] = False

    # Passenger manifest
    passengers: Optional[List[PassengerInput]] = None
    
    # Partial payment option (>=35% and <100%)
    payment_percentage: Optional[float] = 100.0
    
    # Trust Lock Expected Pricing (to prevent mismatch)
    expected_amount: Optional[float] = None
    quick_booking: Optional[bool] = False
    customer_email: Optional[str] = None
    # Payment gateway selection: PHONEPE (default/primary) or CASHFREE (secondary)
    gateway: Optional[str] = "PHONEPE"

@router.post("/checkout")
async def checkout(
    request: CheckoutRequest,
    fastapi_req: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Checkout API (Draft-First Architecture). 
    Validates inventory (locks via reserved_count), applies pricing logic,
    generates a PhonePe payment session, and creates a temporary BookingDraft.
    """
    from app.services.phonepe_client import phonepe_service
    from app.models.booking import BookingDraft
    from app.core.timezone import get_ist_now
    from app.utils.verhoeff import is_valid_aadhaar
    from app.core.config import settings
    from app.models.enums import AccountStatus
    
    if current_user and current_user.account_status in (AccountStatus.BLOCKED, AccountStatus.DISABLED):
        raise HTTPException(status_code=403, detail="Your account is suspended. You cannot make new bookings.")

    # Development hook removed to prevent invoice math corruption.

    # Derive adult, child, and student counts safely
    adult_count = request.adult_count if request.adult_count is not None else request.quantity
    child_count = request.child_count if request.child_count is not None else 0
    student_count = request.student_count if request.student_count is not None else 0

    # Passenger count validation — deferred until after we know if it's a student package
    if not request.passengers or len(request.passengers) != request.quantity:
        raise HTTPException(status_code=400, detail="Passenger details must be provided for all guests")
    
    # ── Passenger validation ──────────────────────────────────────────────────
    from app.models.enums import UserRole
    is_student_pkg = False
    if request.target_type == 'package' and request.variant_id:
        from app.models.package import PackageVariant, Package
        res_stud = await db.execute(
            select(Package.is_student_package)
            .join(PackageVariant, PackageVariant.package_id == Package.id)
            .where(PackageVariant.id == request.variant_id)
        )
        is_student_pkg = bool(res_stud.scalar())

    if request.passengers:
        for i, p in enumerate(request.passengers):
            is_child = i >= adult_count

            if request.quick_booking:

                # Check lead passenger constraints and bypass the rest
                if i == 0:
                    if not is_student_pkg:
                        if p.age < 18:
                            raise HTTPException(status_code=400, detail="Primary passenger must be an adult (18+) for quick booking")
                        if not p.phone:
                            raise HTTPException(status_code=400, detail="Phone number is required for the primary passenger")
                        if not p.aadhaar or not p.aadhaar.strip():
                            raise HTTPException(status_code=400, detail="Aadhaar is required for the primary passenger")
                        if not is_valid_aadhaar(p.aadhaar.strip()):
                            raise HTTPException(status_code=400, detail="Invalid Aadhaar format for the primary passenger")
                    else:
                        # Student package: Aadhaar/phone are optional but validate format if provided
                        if p.phone and p.phone.strip() and len(p.phone.strip()) != 10:
                            raise HTTPException(status_code=400, detail="Contact number must be exactly 10 digits")
                        if p.aadhaar and p.aadhaar.strip() and not is_valid_aadhaar(p.aadhaar.strip()):
                            raise HTTPException(status_code=400, detail="Invalid Aadhaar format for the primary passenger")
                else:
                    continue
            else:
                # Skip strict age/aadhaar for now — re-checked below for non-student packages only
                pass
    has_refreshment_addon = request.has_refreshment_addon or False
    
    subtotal_amount = Decimal("0.00")
    room_variant_id = None
    package_variant_id = None
    _room_stay_dates = []  # Populated for room bookings with multi-day stays
    
    # Start inventory validation scope under SELECT FOR UPDATE
    commissionable_base = Decimal("0.00")

    if request.target_type == 'package':
        from app.core.timezone import get_ist_now
        now_ist = get_ist_now()
        today = now_ist.date()
        is_after_6am = now_ist.hour >= 6
        is_admin = current_user is not None and current_user.role == UserRole.ADMIN
        
        if not is_admin and (request.travel_date < today or (request.travel_date == today and is_after_6am)):
            raise HTTPException(status_code=400, detail="Bookings for today are closed after 6:00 AM IST")

        if not request.variant_id:
            raise HTTPException(status_code=400, detail="variant_id is required for package")
            
        package_variant_id = request.variant_id
        
        # 1. Fetch inventory row with SELECT FOR UPDATE
        inventory_query = (
            select(PackageVariantInventory)
            .where(
                PackageVariantInventory.variant_id == request.variant_id,
                PackageVariantInventory.date == request.travel_date,
                PackageVariantInventory.deleted_at.is_(None)
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
            .options(selectinload(PackageVariant.package))
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
            
        parent_package = variant.package
        
        # ── Agent Quota Enforcement ──────────────────────────────────────────
        is_agent = current_user is not None and current_user.role == UserRole.AGENT
        if is_agent:
            from app.models.user import AgentPackageQuota
            quota_query = select(AgentPackageQuota).where(
                AgentPackageQuota.agent_id == current_user.id,
                AgentPackageQuota.package_id == parent_package.id
            )
            quota_res = await db.execute(quota_query)
            quota = quota_res.scalar_one_or_none()

            daily_limit = quota.daily_quota if quota else 10
            allowed = quota.is_allowed if quota else True

            if not allowed:
                raise HTTPException(status_code=403, detail=f"Booking for the package '{parent_package.title}' is suspended for your account.")

            # Calculate already booked passengers for this agent on this package for this travel date
            booked_query = (
                select(func.sum(Booking.adult_count + Booking.child_count + Booking.student_count))
                .join(PackageVariant, PackageVariant.id == Booking.variant_id)
                .where(
                    Booking.agent_id == current_user.id,
                    PackageVariant.package_id == parent_package.id,
                    Booking.travel_date == request.travel_date,
                    Booking.status.in_((BookingStatus.FULLY_PAID, BookingStatus.PARTIAL_PAID)),
                    Booking.deleted_at.is_(None)
                )
            )
            booked_res = await db.execute(booked_query)
            already_booked = booked_res.scalar() or 0

            requested_quantity = request.quantity
            if already_booked + requested_quantity > daily_limit:
                remaining = max(0, daily_limit - already_booked)
                raise HTTPException(
                    status_code=400,
                    detail=f"You have exceeded your daily quota for '{parent_package.title}' on {request.travel_date}. "
                           f"Daily Limit: {daily_limit}, Already Booked: {already_booked}, "
                           f"Requested: {requested_quantity}, Remaining Allowance: {remaining} tickets."
                )

        is_student_pkg = bool(getattr(parent_package, 'is_student_package', False))

        # ── Pax count validation ─────────────────────────────────────────────
        if is_student_pkg:
            # For student packages: student_count must equal quantity
            student_count = request.student_count if request.student_count is not None else request.quantity
            adult_count = 0
            child_count = 0
            if student_count != request.quantity:
                raise HTTPException(status_code=400, detail="student_count must equal total quantity for student packages")
        else:
            # Normal packages: adult + child must equal quantity
            if (adult_count + child_count) != request.quantity:
                raise HTTPException(status_code=400, detail="Adult and child count must equal total quantity")

        # ── Passenger validation for non-quick, non-student packages ────────
        if not request.quick_booking and not is_student_pkg:
            for i, p in enumerate(request.passengers):
                is_child = i >= adult_count
                if is_child:
                    if not (4 <= p.age <= 10):
                        raise HTTPException(status_code=400, detail=f"Child age must be between 4 and 10 years for passenger {i+1}")
                else:
                    if p.age < 11:
                        raise HTTPException(status_code=400, detail=f"Adult passenger {i+1} must be at least 11 years old")
                    if i == 0 and not p.phone:
                        raise HTTPException(status_code=400, detail="Phone number is required for the primary adult passenger")
                # Aadhaar validation: optional for children <=10, mandatory for 11+
                is_child_age = p.age <= 10
                if not is_child_age:
                    if not p.aadhaar or not p.aadhaar.strip():
                        raise HTTPException(status_code=400, detail=f"Aadhaar is required for passenger {i+1} (age 11+)")
                    if not is_valid_aadhaar(p.aadhaar.strip()):
                        raise HTTPException(status_code=400, detail=f"Invalid Aadhaar format for passenger {i+1}")
                else:
                    if p.aadhaar and p.aadhaar.strip() and not is_valid_aadhaar(p.aadhaar.strip()):
                        raise HTTPException(status_code=400, detail=f"Invalid Aadhaar format for passenger {i+1}")
        elif not request.quick_booking and is_student_pkg:
            # Student packages: Aadhaar optional but validate format if provided
            for i, p in enumerate(request.passengers):
                if p.aadhaar and p.aadhaar.strip() and not is_valid_aadhaar(p.aadhaar.strip()):
                    raise HTTPException(status_code=400, detail=f"Invalid Aadhaar format for passenger {i+1}")

        # ── Minimum passengers check ───────────────────────────────────
        min_pax = getattr(parent_package, 'min_passengers', 1) or 1
        total_passengers = student_count if is_student_pkg else (adult_count + child_count)
        if total_passengers < min_pax:
            raise HTTPException(
                status_code=400,
                detail=f"This package requires a minimum of {min_pax} passengers per booking. You have selected {total_passengers}."
            )

        # ── Pricing ──────────────────────────────────────────────────
        is_weekend = request.travel_date.weekday() in (5, 6)

        if is_student_pkg:
            # Student pricing: one price per student head
            base_student_price = getattr(variant, 'student_price', None) or Decimal("0.00")
            if is_weekend and getattr(variant, 'weekend_student_price', None):
                base_student_price = variant.weekend_student_price
            if inventory.price_override is not None:
                base_student_price = inventory.price_override
            eff_student = Decimal(str(base_student_price))
            eff_adult = eff_student  # used for SSE payload compatibility
            eff_child = Decimal("0.00")
            base_subtotal = Decimal(str(student_count)) * eff_student
        else:
            base_adult_price = variant.adult_price
            base_child_price = variant.child_price
            if is_weekend:
                if variant.weekend_adult_price is not None:
                    base_adult_price = variant.weekend_adult_price
                if variant.weekend_child_price is not None:
                    base_child_price = variant.weekend_child_price
            eff_adult, eff_child = get_effective_package_prices(
                base_adult_price, base_child_price, inventory.price_override
            )
            base_subtotal = Decimal(str(adult_count)) * eff_adult + Decimal(str(child_count)) * eff_child

        transport_subtotal = Decimal("0.00")
        transport_snapshot_items = []
        transport_options_to_broadcast = []

        # Resolve transport_selections (new) or fall back to legacy transport_option_id
        effective_selections = request.transport_selections or []
        if not effective_selections and request.transport_option_id:
            effective_selections = [TransportSelection(option_id=request.transport_option_id, quantity=1)]

        if parent_package.has_transport and not effective_selections:
            raise HTTPException(
                status_code=400,
                detail="Transport selection is mandatory for this package."
            )

        if parent_package.has_transport and effective_selections:
            selected_opt_ids = [s.option_id for s in effective_selections]
            t_opts_res = await db.execute(
                select(PackageTransportOption).where(
                    PackageTransportOption.id.in_(selected_opt_ids),
                    PackageTransportOption.package_id == parent_package.id
                )
            )
            t_opts_map = {t.id: t for t in t_opts_res.scalars().all()}

            for sel in effective_selections:
                t_opt = t_opts_map.get(sel.option_id)
                if not t_opt:
                    raise HTTPException(status_code=400, detail=f"Invalid transport option id: {sel.option_id}")

                if t_opt.type == 'SHARED':
                    if is_student_pkg:
                        # Student SHARED: one price per student
                        t_student = getattr(t_opt, 'student_price', None) or Decimal("0.00")
                        if is_weekend and getattr(t_opt, 'weekend_student_price', None):
                            t_student = t_opt.weekend_student_price
                        item_cost = Decimal(str(student_count)) * t_student
                        transport_subtotal += item_cost
                        transport_snapshot_items.append({
                            "option_id": t_opt.id, "title": t_opt.title, "type": "SHARED",
                            "capacity": int(t_opt.capacity or 0), "quantity": 1,
                            "student_price": float(t_student), "item_total": float(item_cost)
                        })
                    else:
                        t_adult = t_opt.adult_price or Decimal("0.00")
                        t_child = t_opt.child_price or Decimal("0.00")
                        if is_weekend:
                            if t_opt.weekend_adult_price is not None:
                                t_adult = t_opt.weekend_adult_price
                            if t_opt.weekend_child_price is not None:
                                t_child = t_opt.weekend_child_price
                        item_cost = Decimal(str(adult_count)) * t_adult + Decimal(str(child_count)) * t_child
                        transport_subtotal += item_cost
                        transport_snapshot_items.append({
                            "option_id": t_opt.id, "title": t_opt.title, "type": "SHARED",
                            "capacity": int(t_opt.capacity or 0), "quantity": 1,
                            "adult_price": float(t_adult), "child_price": float(t_child), "item_total": float(item_cost)
                        })

                elif t_opt.type == 'SEPARATE_VEHICLE':
                    t_fixed = t_opt.fixed_price or Decimal("0.00")
                    if is_weekend and t_opt.weekend_fixed_price is not None:
                        t_fixed = t_opt.weekend_fixed_price
                    item_cost = Decimal(str(sel.quantity)) * t_fixed
                    transport_subtotal += item_cost
                    transport_snapshot_items.append({
                        "option_id": t_opt.id, "title": t_opt.title, "type": "SEPARATE_VEHICLE",
                        "capacity": int(t_opt.capacity or 0), "quantity": sel.quantity,
                        "fixed_price": float(t_fixed), "item_total": float(item_cost)
                    })

            # Validate separate vehicle capacity
            total_pax = student_count if is_student_pkg else (adult_count + child_count)
            separate_capacity = sum(
                s.quantity * (t_opts_map[s.option_id].capacity or 1)
                for s in effective_selections
                if s.option_id in t_opts_map and t_opts_map[s.option_id].type == 'SEPARATE_VEHICLE'
            )
            has_separate = any(
                s.option_id in t_opts_map and t_opts_map[s.option_id].type == 'SEPARATE_VEHICLE'
                for s in effective_selections
            )
            if has_separate and separate_capacity < total_pax:
                raise HTTPException(
                    status_code=400,
                    detail=f"Selected vehicles can only seat {separate_capacity} passengers, but you have {total_pax}. Please add more vehicles."
                )

            # ── Transport Inventory Availability Check ──────────────────────
            from app.models.package import PackageTransportInventory
            travel_date_obj = request.travel_date if hasattr(request.travel_date, 'year') else \
                __import__('datetime').date.fromisoformat(str(request.travel_date))

            for sel in effective_selections:
                t_opt = t_opts_map.get(sel.option_id)
                if not t_opt:
                    continue

                inv_row = await db.scalar(
                    select(PackageTransportInventory).where(
                        PackageTransportInventory.transport_option_id == sel.option_id,
                        PackageTransportInventory.date == travel_date_obj,
                        PackageTransportInventory.deleted_at.is_(None),
                    ).with_for_update()
                )

                if inv_row is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Transport option '{t_opt.title}' is not available on {travel_date_obj}. The admin has not opened this transport for this date."
                    )

                if inv_row.is_closed:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Transport option '{t_opt.title}' is closed for {travel_date_obj}."
                    )

                t_type_str = t_opt.type.value if hasattr(t_opt.type, 'value') else str(t_opt.type)
                if t_type_str == 'SEPARATE_VEHICLE':
                    remaining = inv_row.available_count - inv_row.booked_count
                    if sel.quantity > remaining:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Not enough '{t_opt.title}' vehicles available on {travel_date_obj}. Requested: {sel.quantity}, Available: {remaining}."
                        )
                    inv_row.booked_count += sel.quantity
                else:  # SHARED — consume passenger seats
                    seats_needed = student_count if is_student_pkg else (adult_count + child_count)
                    total_seats = inv_row.available_count * (t_opt.capacity or 1)
                    remaining = total_seats - inv_row.booked_count
                    if seats_needed > remaining:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Not enough seats on '{t_opt.title}' for {travel_date_obj}. Needed: {seats_needed}, Remaining: {remaining}."
                        )
                    inv_row.booked_count += seats_needed
                transport_options_to_broadcast.append(sel.option_id)
            # ── End Transport Inventory Check ────────────────────────────────

        refreshment_subtotal = Decimal("0.00")
        if parent_package.has_refreshments and request.include_refreshments:
            if is_student_pkg:
                r_student = getattr(parent_package, 'refreshment_student_price', None) or \
                            parent_package.refreshment_adult_price or Decimal("0.00")
                refreshment_subtotal = Decimal(str(student_count)) * Decimal(str(r_student))
            else:
                r_adult = parent_package.refreshment_adult_price or Decimal("0.00")
                r_child = parent_package.refreshment_child_price or Decimal("0.00")
                refreshment_subtotal = Decimal(str(adult_count)) * r_adult + Decimal(str(child_count)) * r_child

        subtotal_amount = base_subtotal + transport_subtotal + refreshment_subtotal
        
        # Hook removed.
            
        commissionable_base = base_subtotal

        # Store student metadata in pricing snapshot for invoice/ticket rendering
        if is_student_pkg:
            _student_snapshot = {
                "is_student_package": True,
                "student_count": student_count,
                "student_price_per_head": float(eff_student) if is_student_pkg else 0.0,
            }
        else:
            _student_snapshot = {"is_student_package": False}

        # Reserve inventory
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

        from app.core.timezone import get_ist_now
        now_ist = get_ist_now()
        today = now_ist.date()
        is_after_6am = now_ist.hour >= 6
        is_admin = current_user is not None and current_user.role == UserRole.ADMIN
        
        if not is_admin and (arrival < today or (arrival == today and is_after_6am)):
            raise HTTPException(status_code=400, detail="Bookings for today are closed after 6:00 AM IST")

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
                    RoomSlotInventory.slot_end == request.slot_end,
                    RoomSlotInventory.deleted_at.is_(None)
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
            
        # Hook removed.
            subtotal_amount += Decimal(str(required_rooms)) * day_price
        commissionable_base = subtotal_amount

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
            ticket_count=request.quantity,
            travel_date=request.travel_date
        ):
            raise HTTPException(status_code=400, detail="Invalid or expired promo code.")
            
        coupon_discount = Decimal(str(coupon.calculate_discount(float(subtotal_amount))))
        coupon_applied = coupon.code
        # IMPORTANT: Do not increment usage_count yet! Deferred to webhook confirmation.
        
    discounted_subtotal = max(Decimal("0.00"), subtotal_amount - coupon_discount)
    gst_amount = (discounted_subtotal * Decimal("0.05")).quantize(Decimal("0.01"))
    gateway_fee = ((discounted_subtotal + gst_amount) * Decimal("0.01")).quantize(Decimal("0.01"))
    total_amount = discounted_subtotal + gst_amount + gateway_fee
    
    # Overrides removed to fix invoice generation.
    
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
            agent_discount = min(fixed_amount, commissionable_base, total_amount).quantize(Decimal("0.01"))
        else:
            # PERCENTAGE type
            agent_discount = (
                commissionable_base
                * commission_percentage
                / Decimal("100")
            ).quantize(Decimal("0.01"))
            # Clamp: never exceed the total
            agent_discount = min(agent_discount, total_amount)
        
        agent_payable = max(Decimal("0.00"), total_amount - agent_discount)
        
    # Construct historical pricing snapshot with Decimal-safe string values
    pricing_snapshot = {
        "subtotal_amount": str(subtotal_amount),
        "refreshment_subtotal": str(refreshment_subtotal) if 'refreshment_subtotal' in locals() else "0.00",
        "has_refreshment_addon": getattr(request, 'include_refreshments', False) or getattr(request, 'has_refreshment_addon', False),
        "coupon_discount": str(coupon_discount),
        "coupon_applied": coupon_applied,
        "gst_amount": str(gst_amount),
        "gateway_fee": str(gateway_fee),
        "tourist_total": str(total_amount),
        "commissionable_base": str(commissionable_base),
        "agent_discount": str(agent_discount),
        "agent_payable": str(agent_payable),
        "booking_mode": "QUICK" if request.quick_booking else "FULL",
        **(_student_snapshot if '_student_snapshot' in locals() else {"is_student_package": False}),
    }
    # Store transport selections breakdown in snapshot for invoice/ticket rendering
    if request.target_type == 'package' and transport_snapshot_items:
        pricing_snapshot["transport_selections"] = transport_snapshot_items

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
        
    # --- Payment Gateway Order Generation ---
    payment_percentage = request.payment_percentage if request.payment_percentage is not None else 100.0
    if not (35.0 <= payment_percentage <= 100.0):
        raise HTTPException(status_code=400, detail="Payment percentage must be between 35% and 100%")
    
    tourist_amount_payable = (total_amount * Decimal(str(payment_percentage)) / Decimal("100")).quantize(Decimal("0.01"))
    
    user_phone = None
    if request.passengers:
        user_phone = next((p.phone for p in request.passengers if p.phone), None)

    user_name = None
    user_email = None
    if request.passengers:
        lead = request.passengers[0]
        user_name = lead.full_name
    if request.customer_email:
        user_email = request.customer_email
    elif current_user and current_user.email:
        user_email = current_user.email

    if is_agent:
        payable_amount = (agent_payable * Decimal(str(payment_percentage)) / Decimal("100")).quantize(Decimal("0.01"))
    else:
        payable_amount = tourist_amount_payable

    # Developer test bypass (only active in development/staging environments)
    if settings.ENVIRONMENT != "production" and (user_email == '2024eb01987@online.bits-pilani.ac.in' or user_phone == '8886154275'):
        payable_amount = Decimal("1.00")
        total_amount = Decimal("1.00")
        tourist_amount_payable = Decimal("1.00")
        pricing_snapshot["tourist_total"] = "1.00"
        pricing_snapshot["tourist_amount_payable"] = "1.00"

    pricing_snapshot["payment_percentage"] = str(payment_percentage)
    pricing_snapshot["tourist_amount_payable"] = str(tourist_amount_payable)
    pricing_snapshot["actual_paid_advance"] = str(payable_amount)

    if request.expected_amount is not None:
        pass  # Frontend sends expected_amount but we process checkout with realtime calculated price.

    draft_id = "DRF-" + str(uuid.uuid4())[:8].upper()
    merchant_txn_id = f"TXN_{draft_id}_{str(uuid.uuid4())[:4].upper()}"

    host = fastapi_req.headers.get('host')
    protocol = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"

    # user_phone and user_email extracted above

    # Determine selected gateway (default: PHONEPE)
    selected_gateway = (request.gateway or "PHONEPE").upper()
    if selected_gateway not in ("PHONEPE", "CASHFREE"):
        selected_gateway = "PHONEPE"

    # --- Gateway-specific order creation ---
    checkout_response = {}

    if selected_gateway == "CASHFREE":
        from app.services.cashfree_client import cashfree_service
        cashfree_return_url = f"{settings.FRONTEND_URL}/payment-status?merchantTransactionId={merchant_txn_id}&gateway=CASHFREE"
        cf_order = await cashfree_service.create_order(
            order_id=merchant_txn_id,
            amount=float(payable_amount),
            customer_id=str(current_user.id) if current_user else "guest",
            customer_name=user_name or "Customer",
            customer_email=user_email or "noreply@tsboattourism.org",
            customer_phone=user_phone or "9999999999",
            return_url=cashfree_return_url,
        )
        checkout_response = {
            "gateway": "CASHFREE",
            "payment_session_id": cf_order.get("payment_session_id"),
            "pg_transaction_id": merchant_txn_id,
            "amount": int(float(payable_amount) * 100),  # in paise for consistency
            "currency": "INR",
            "mode": cashfree_service.env.lower(),
        }
    else:
        # Default: PhonePe
        callback_url = f"{protocol}://{host}/api/v1/payments/webhook/phonepe"
        redirect_url = f"{settings.FRONTEND_URL}/payment-status?merchantTransactionId={merchant_txn_id}&gateway=PHONEPE"
        phonepe_order = await phonepe_service.create_payment_url(
            amount=float(payable_amount),
            transaction_id=merchant_txn_id,
            user_id=str(current_user.id) if current_user else "guest",
            redirect_url=redirect_url,
            callback_url=callback_url,
            phone_number=user_phone
        )
        checkout_response = {
            "gateway": "PHONEPE",
            "redirect_url": phonepe_order.get("redirect_url"),
            "pg_transaction_id": merchant_txn_id,
            "amount": phonepe_order.get("amount"),  # in paise
            "currency": "INR",
        }

    # --- Database Draft Persistence ---
    now = get_ist_now()
    expires_at = now + timedelta(minutes=5)

    payload_dump = request.model_dump(mode='json')

    draft = BookingDraft(
        draft_id=draft_id,
        pg_transaction_id=merchant_txn_id,
        payment_gateway=selected_gateway,
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

    from app.utils.sse import broadcast_transport_update
    for opt_id in transport_options_to_broadcast:
        await broadcast_transport_update(db, opt_id, request.travel_date)

    for p in sse_payloads:
        target_channel = "package" if request.target_type.lower() == 'package' else "room"
        target_id = p.get("package_id") if target_channel == "package" else p.get("room_id")
        await sse_manager.broadcast_event(target_channel, str(target_id), "INVENTORY_UPDATE", p)

    logger.info(f"BookingDraft {draft_id} created | gateway={selected_gateway} | order={merchant_txn_id} | expires={expires_at.isoformat()}")

    return {
        "status": "success",
        "message": "Draft created and inventory reserved.",
        "checkout_data": {
            "draft_id": draft.draft_id,
            **checkout_response,
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
        select(
            Booking, 
            Package.title, 
            PackageVariant.title,
            Room.lodge_name,
            RoomVariant.variant_name,
            Room.slot_start,
            Room.slot_end,
            PackageBoardingPoint.departure_time
        )
        .outerjoin(PackageVariant, Booking.variant_id == PackageVariant.id)
        .outerjoin(Package, PackageVariant.package_id == Package.id)
        .outerjoin(PackageBoardingPoint, (PackageBoardingPoint.package_id == Package.id) & (PackageBoardingPoint.sort_order == 0))
        .outerjoin(RoomVariant, Booking.room_variant_id == RoomVariant.id)
        .outerjoin(Room, RoomVariant.room_id == Room.id)
        .options(selectinload(Booking.passengers))
        .options(selectinload(Booking.stay_dates))
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
    from datetime import datetime, timedelta
    for row in rows:
        b = row[0]
        package_title = row[1] if b.variant_id else row[3]
        variant_title = row[2] if b.variant_id else row[4]
        
        target_type = "ROOM" if b.room_variant_id else "PACKAGE"
        room_checkin = None
        room_checkout = None
        room_checkout_date = None
        package_departure_time = None
        
        if target_type == "ROOM":
            if b.stay_dates:
                dates = [sd.date for sd in b.stay_dates]
                if dates:
                    room_checkout_date = (max(dates) + timedelta(days=1)).isoformat()
            
            if b.pricing_snapshot and b.pricing_snapshot.get('slot_start'):
                try:
                    room_checkin = datetime.strptime(b.pricing_snapshot.get('slot_start'), "%H:%M:%S").strftime('%I:%M %p')
                    room_checkout = datetime.strptime(b.pricing_snapshot.get('slot_end'), "%H:%M:%S").strftime('%I:%M %p')
                except:
                    pass
            if not room_checkin:
                room_checkin = row[5].strftime('%I:%M %p') if row[5] else None
                room_checkout = row[6].strftime('%I:%M %p') if row[6] else None
        else:
            package_departure_time = row[7].strftime('%I:%M %p') if row[7] else None

        sanitized_items.append({
            "target_type": target_type,
            "room_checkin": room_checkin,
            "room_checkout": room_checkout,
            "room_checkout_date": room_checkout_date,
            "package_departure_time": package_departure_time,
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
            "has_refreshment_addon": getattr(b, 'has_refreshment_addon', False),
            "status": b.status.value if hasattr(b.status, 'value') else str(b.status),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "package_title": package_title,
            "variant_title": variant_title,
            "passenger_names": [p.full_name for p in b.passengers],
            "agent_commission": float(b.agent_commission or 0),
            "pricing_snapshot": b.pricing_snapshot,
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
        select(
            Booking, 
            Package.title, 
            PackageVariant.title,
            Room.lodge_name,
            RoomVariant.variant_name,
            Room.slot_start,
            Room.slot_end,
            PackageBoardingPoint.departure_time
        )
        .outerjoin(PackageVariant, Booking.variant_id == PackageVariant.id)
        .outerjoin(Package, PackageVariant.package_id == Package.id)
        .outerjoin(PackageBoardingPoint, (PackageBoardingPoint.package_id == Package.id) & (PackageBoardingPoint.sort_order == 0))
        .outerjoin(RoomVariant, Booking.room_variant_id == RoomVariant.id)
        .outerjoin(Room, RoomVariant.room_id == Room.id)
        .options(selectinload(Booking.passengers))
        .options(selectinload(Booking.stay_dates))
        .options(selectinload(Booking.postpone_requests))
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
    from datetime import datetime, timedelta
    for row in rows:
        b = row[0]
        package_title = row[1] if b.variant_id else row[3]
        variant_title = row[2] if b.variant_id else row[4]
        
        target_type = "ROOM" if b.room_variant_id else "PACKAGE"
        room_checkin = None
        room_checkout = None
        room_checkout_date = None
        package_departure_time = None
        
        if target_type == "ROOM":
            if b.stay_dates:
                dates = [sd.date for sd in b.stay_dates]
                if dates:
                    room_checkout_date = (max(dates) + timedelta(days=1)).isoformat()
            
            if b.pricing_snapshot and b.pricing_snapshot.get('slot_start'):
                try:
                    room_checkin = datetime.strptime(b.pricing_snapshot.get('slot_start'), "%H:%M:%S").strftime('%I:%M %p')
                    room_checkout = datetime.strptime(b.pricing_snapshot.get('slot_end'), "%H:%M:%S").strftime('%I:%M %p')
                except:
                    pass
            if not room_checkin:
                room_checkin = row[5].strftime('%I:%M %p') if row[5] else None
                room_checkout = row[6].strftime('%I:%M %p') if row[6] else None
        else:
            package_departure_time = row[7].strftime('%I:%M %p') if row[7] else None

        _b_public_paid = b.paid_amount
        _b_public_remaining = b.remaining_balance

        sanitized_items.append({
            "target_type": target_type,
            "room_checkin": room_checkin,
            "room_checkout": room_checkout,
            "room_checkout_date": room_checkout_date,
            "package_departure_time": package_departure_time,
            "id": b.id,
            "public_id": b.public_id,
            "travel_date": b.travel_date.isoformat(),
            "adult_count": b.adult_count,
            "child_count": b.child_count,
            "total_amount": float(b.total_amount),
            "subtotal_amount": float(b.subtotal_amount),
            "coupon_discount": float(b.coupon_discount),
            "coupon_applied": b.coupon_applied,
            "gst_amount": float(b.gst_amount),
            "gateway_fee": float(b.gateway_fee),
            "paid_amount": float(_b_public_paid),
            "remaining_balance": float(_b_public_remaining),
            "has_refreshment_addon": getattr(b, 'has_refreshment_addon', False),
            "status": b.status.value if hasattr(b.status, 'value') else str(b.status),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "package_title": package_title,
            "variant_title": variant_title,
            "passenger_names": [p.full_name for p in b.passengers],
            "pricing_snapshot": b.pricing_snapshot,
            "is_rescheduled": any((r.status.value if hasattr(r.status, "value") else str(r.status)) == "APPROVED" for r in b.postpone_requests),
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
    response: Response,
    secret: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Retrieve booking detail by its public ID.
    Commission fields are ONLY included when the authenticated user owns the booking as an agent or is an admin.
    Public users and tourists never receive agent_commission or agent_payable.
    """
    from app.utils.cache import set_no_store_headers
    set_no_store_headers(response)
    
    from app.models.room import Room, RoomVariant
    query = (
        select(Booking, Package.title, PackageVariant.title, Room.lodge_name, RoomVariant.variant_name, Room.slot_start, Room.slot_end, Room.address, Room.id, Package.type)
        .outerjoin(PackageVariant, Booking.variant_id == PackageVariant.id)
        .outerjoin(Package, PackageVariant.package_id == Package.id)
        .outerjoin(RoomVariant, Booking.room_variant_id == RoomVariant.id)
        .outerjoin(Room, RoomVariant.room_id == Room.id)
        .options(selectinload(Booking.passengers))
        .options(selectinload(Booking.stay_dates))
        .options(selectinload(Booking.cancellation_requests))
        .options(selectinload(Booking.postpone_requests))
        .options(selectinload(Booking.agent))
        .options(selectinload(Booking.customer))
        .options(selectinload(Booking.package_variant))
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
    package_type = row[9] if b.variant_id else None
    
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
    room_id = row[8]
    
    room_checkout_date = None
    room_highlights = []
    if b.room_variant_id:
        if b.stay_dates:
            dates = [sd.date for sd in b.stay_dates]
            if dates:
                room_checkout_date = (max(dates) + timedelta(days=1)).isoformat()
        if room_id:
            from app.models.room import RoomHighlight
            hi_query = select(RoomHighlight).where(RoomHighlight.room_id == room_id).order_by(RoomHighlight.sort_order.asc())
            hi_res = await db.execute(hi_query)
            room_highlights = [{"title": hi.title, "icon": hi.icon} for hi in hi_res.scalars().all()]
    
    agent_id = None
    agent_name = None
    agent_phone = None
    agent_gst = None
    agent_company = None
    if b.agent_id:
        agent = b.agent
        if agent:
            agent_id = agent.id
            agent_name = agent.full_name
            agent_phone = agent.phone_number
            agent_gst = agent.gst_number
            agent_company = agent.company_name
    else:
        agent_gst = None
        agent_company = None

    booked_by_name = None
    booked_by_email = None
    booked_by_role = None
    if b.user_id:
        booked_user = b.customer
        if booked_user:
            booked_by_name = booked_user.full_name
            booked_by_email = booked_user.email
            booked_by_role = booked_user.role.value if hasattr(booked_user.role, 'value') else str(booked_user.role)

    boarding_point = None
    itinerary = []
    if b.variant_id:
        from app.models.package import PackageBoardingPoint, PackageItineraryDay
        variant = b.package_variant
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
    # Use already-loaded relationships to avoid enum type mismatch in raw SQL
    has_pending_cancellation = any(
        (r.status.value if hasattr(r.status, "value") else str(r.status)) == "PENDING"
        for r in b.cancellation_requests
    )
    has_pending_postpone = any(
        (r.status.value if hasattr(r.status, "value") else str(r.status)) == "PENDING"
        for r in b.postpone_requests
    )

    # ─── Build Payment Ledger ─────────────────────────────────────────────────
    from app.models.payment import Payment
    from app.models.enums import PaymentStatus
    payment_ledger_stmt = select(Payment).where(
        Payment.booking_id == b.id,
        Payment.deleted_at.is_(None)
    ).order_by(Payment.created_at.asc())
    p_result = await db.execute(payment_ledger_stmt)
    raw_payments = p_result.scalars().all()

    # ─── Commission Gate: Only for owning agent or admin ─────────────────────
    import hmac
    import hashlib
    from app.core.config import settings

    is_agent_owner = (
        current_user is not None
        and current_user.role == UserRole.AGENT
        and b.agent_id == current_user.id
    )
    is_admin = current_user is not None and current_user.role == UserRole.ADMIN
    
    is_valid_secret = False
    if secret:
        secret_key = settings.SECRET_KEY or 'tsaptourismpapikondalubadhrachalam'
        expected = hmac.new(
            secret_key.encode('utf-8'),
            public_id.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(secret, expected):
            is_valid_secret = True
            
    show_commission = is_agent_owner or is_admin or is_valid_secret
    use_agent_payment_view = show_commission and b.agent_id is not None

    # Calculate scale ratio for public view of agent bookings to prevent leaks
    scale_ratio = Decimal("1.0")
    is_agent = b.agent_id is not None and b.agent_commission is not None and b.agent_commission > 0
    if is_agent and not show_commission:
        agent_payable = max(Decimal("0.00"), Decimal(str(b.total_amount)) - Decimal(str(b.agent_commission)))
        if agent_payable > 0:
            scale_ratio = Decimal(str(b.total_amount)) / agent_payable

    payment_ledger = []
    for p in raw_payments:
        collected_by_label = p.collected_by_label
        if not collected_by_label:
            if p.collected_by_type == "RAZORPAY":
                collected_by_label = "PhonePe (Legacy)"
            elif p.collected_by_type == "PHONEPE":
                collected_by_label = "PhonePe"
            elif p.collected_by_type == "CASHFREE":
                collected_by_label = "Cashfree"
            else:
                collected_by_label = "Admin (Cash)"

        raw_amt = Decimal(str(p.amount))
        scaled_amt = (raw_amt * scale_ratio).quantize(Decimal("0.01"))

        payment_ledger.append({
            "id": p.id,
            "amount": float(scaled_amt),
            "payment_method": p.payment_method,
            "status": p.status.value if hasattr(p.status, "value") else str(p.status),
            "collected_by_type": p.collected_by_type,
            "collected_by_label": collected_by_label,
            "payment_reference_id": p.payment_reference_id,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })

    agent_paid = sum(Decimal(str(p.amount)) for p in raw_payments if p.status == PaymentStatus.CAPTURED)
    agent_payable = max(Decimal("0.00"), Decimal(str(b.total_amount)) - Decimal(str(b.agent_commission or "0.00")))

    result_dict = {
        "id": b.id,
        "public_id": b.public_id,
        "customer_email": b.customer_email,
        "target_type": "ROOM" if b.room_variant_id else "PACKAGE",
        "travel_date": b.travel_date.isoformat(),
        "adult_count": b.adult_count,
        "child_count": b.child_count,
        "student_count": b.student_count,
        "subtotal_amount": float(b.subtotal_amount),
        "has_refreshment_addon": getattr(b, 'has_refreshment_addon', False),
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
        "package_type": package_type.value if hasattr(package_type, 'value') else str(package_type) if package_type else None,
        "boarding_point": {
            "title": boarding_point.title,
            "address": boarding_point.address,
            "landmark": boarding_point.landmark,
            "departure_time": boarding_point.departure_time,
            "contact_number": boarding_point.contact_number
        } if boarding_point else None,
        "room_checkin": room_checkin,
        "room_checkout": room_checkout,
        "room_checkout_date": room_checkout_date,
        "room_address": room_address,
        "room_highlights": room_highlights,
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
                "student_class": p.student_class,
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
        "booked_by_name": booked_by_name,
        "booked_by_email": booked_by_email,
        "booked_by_role": booked_by_role,
        "has_pending_cancellation": has_pending_cancellation,
        "has_pending_postpone": has_pending_postpone,
        "is_rescheduled": any((r.status.value if hasattr(r.status, "value") else str(r.status)) == "APPROVED" for r in b.postpone_requests),
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
        "invoice_secret": (
            hmac.new(
                (settings.SECRET_KEY or 'tsaptourismpapikondalubadhrachalam').encode('utf-8'),
                b.public_id.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            if show_commission
            else None
        ),
        # Immutable payment history — always returned
        "payment_ledger": payment_ledger,
        "pricing_snapshot": b.pricing_snapshot,
    }
    
    # Include cancellation details if present
    if b.cancellation_requests:
        latest_cancel = sorted(b.cancellation_requests, key=lambda c: c.requested_at, reverse=True)[0]
        result_dict["cancellation_details"] = {
            "status": latest_cancel.status.value if hasattr(latest_cancel.status, "value") else str(latest_cancel.status),
            "reason": latest_cancel.reason,
            "cancellation_fee": float(latest_cancel.cancellation_fee) if latest_cancel.cancellation_fee is not None else None,
            "refund_amount": float(latest_cancel.refund_amount) if latest_cancel.refund_amount is not None else None,
            "requested_at": latest_cancel.requested_at.isoformat() if latest_cancel.requested_at else None,
            "processed_at": latest_cancel.processed_at.isoformat() if latest_cancel.processed_at else None,
        }

    # Include postponement details if present
    if b.postpone_requests:
        latest_postpone = sorted(b.postpone_requests, key=lambda p: p.requested_at, reverse=True)[0]
        result_dict["postpone_details"] = {
            "status": latest_postpone.status.value if hasattr(latest_postpone.status, "value") else str(latest_postpone.status),
            "reason": latest_postpone.reason,
            "requested_new_date": latest_postpone.requested_new_date.isoformat() if latest_postpone.requested_new_date else None,
            "original_travel_date": latest_postpone.original_travel_date.isoformat() if latest_postpone.original_travel_date else None,
            "requested_at": latest_postpone.requested_at.isoformat() if latest_postpone.requested_at else None,
            "processed_at": latest_postpone.processed_at.isoformat() if latest_postpone.processed_at else None,
        }
        
    return result_dict

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

    # Enforce 4-day cancellation restriction (travel_date > current_date + 4 days)
    today = get_ist_now().date()
    if booking.travel_date < today + timedelta(days=4):
        raise HTTPException(
            status_code=400,
            detail="Cancellation unavailable within 4 days of travel"
        )

    # Check for pending cancellation requests
    pending_query = select(CancellationRequest).where(
        CancellationRequest.booking_id == booking.id,
        CancellationRequest.status == "PENDING"
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

class PostponeRequestInput(BaseModel):
    requested_new_date: date
    reason: str = Field(..., min_length=5, max_length=1000)

@router.post("/{public_id}/postpone")
async def request_booking_postpone(
    public_id: str,
    req: PostponeRequestInput,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """
    Tourist/Agent requests booking postponement (reschedule):
    1. Fetch booking by public ID.
    2. Check ownership.
    3. Ensure booking status is CONFIRMED, FULLY_PAID, or PARTIAL_PAID.
    4. Enforce 7-day postpone restriction (travel_date >= today + 7 days).
    5. Create PostponeRequest in PENDING state.
    """
    from app.models.booking import Booking, PostponeRequest
    from app.models.enums import CancellationStatus, BookingStatus
    from datetime import date, timedelta
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
        raise HTTPException(status_code=401, detail="Authentication required to request postponement")

    is_owner = (booking.user_id == current_user.id or booking.agent_id == current_user.id)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Not authorized to postpone this booking")

    if booking.status not in (BookingStatus.FULLY_PAID, BookingStatus.PARTIAL_PAID):
        raise HTTPException(status_code=400, detail="Only paid bookings can be postponed")

    # Enforce 7-day postpone restriction (travel_date >= today + 7 days)
    today = get_ist_now().date()
    if booking.travel_date < today + timedelta(days=7):
        raise HTTPException(
            status_code=400,
            detail="Postponement unavailable within 7 days of travel"
        )

    # Ensure proposed date is in the future
    if req.requested_new_date <= today:
        raise HTTPException(status_code=400, detail="Proposed travel date must be in the future")

    # Check for pending or approved postponement requests
    pending_query = select(PostponeRequest).where(
        PostponeRequest.booking_id == booking.id,
        PostponeRequest.status.in_(["PENDING", "APPROVED"])
    ).limit(1)
    p_result = await db.execute(pending_query)
    existing_request = p_result.scalar_one_or_none()
    if existing_request:
        if existing_request.status == "APPROVED":
            raise HTTPException(status_code=400, detail="This booking has already been postponed once. Further postponements are not allowed.")
        raise HTTPException(status_code=400, detail="A postponement request is already pending for this booking")

    postpone_req = PostponeRequest(
        booking_id=booking.id,
        reason=req.reason,
        requested_new_date=req.requested_new_date,
        original_travel_date=booking.travel_date,
        status=CancellationStatus.PENDING
    )
    db.add(postpone_req)
    await db.commit()

    return {
        "status": "success",
        "message": "Postponement request submitted successfully and is pending admin review."
    }

@router.post("/{public_id}/balance-checkout")
async def process_balance_checkout(
    public_id: str,
    fastapi_req: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    """
    Tourist balance checkout — supports PhonePe and Cashfree.
    Generates a payment session for the remaining balance.
    """
    from app.services.phonepe_client import phonepe_service
    from app.services.cashfree_client import cashfree_service
    from app.models.payment import Payment
    from app.models.enums import PaymentStatus, AccountStatus
    from app.core.config import settings

    if current_user and current_user.account_status in (AccountStatus.BLOCKED, AccountStatus.DISABLED):
        raise HTTPException(status_code=403, detail="Your account is suspended. You cannot make new bookings.")

    # Fetch booking
    query = (
        select(Booking)
        .options(
            selectinload(Booking.payments),
            selectinload(Booking.passengers)
        )
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

    # Cancel any previous CREATED payment records to ensure a fresh session
    for p in booking.payments:
        if p.status == PaymentStatus.CREATED:
            p.status = PaymentStatus.FAILED
            p.error_code = "ORDER_SUPERSEDED"
            p.error_description = "Superseded by a new balance payment checkout session."

    # Generate a new PhonePe payment session for remaining balance
    is_agent_payment = current_user is not None and current_user.role == UserRole.AGENT and booking.agent_id == current_user.id
    
    if is_agent_payment:
        agent_payable = Decimal(str(booking.total_amount)) - Decimal(str(booking.agent_commission or "0.00"))
        captured_total = sum(Decimal(str(p.amount)) for p in booking.payments if p.status == PaymentStatus.CAPTURED)
        payable_amount = float(max(Decimal("0.00"), agent_payable - captured_total))
    else:
        payable_amount = float(booking.remaining_balance)
        
    # Determine gateway from query param (default PhonePe)
    gateway_param = fastapi_req.query_params.get("gateway", "PHONEPE").upper()
    if gateway_param not in ("PHONEPE", "CASHFREE"):
        gateway_param = "PHONEPE"

    host = fastapi_req.headers.get('host')
    protocol = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"

    user_phone = None
    if booking.passengers:
        user_phone = next((p.phone_number for p in booking.passengers if p.phone_number), None)

    # Get customer info for Cashfree
    customer_name = None
    customer_email = None
    if booking.passengers:
        lead = next((p for p in booking.passengers if p.is_primary), booking.passengers[0])
        customer_name = lead.full_name
    if current_user and current_user.email:
        customer_email = current_user.email

    import uuid
    merchant_txn_id = f"TXN_BAL_{str(uuid.uuid4())[:8].upper()}_{str(uuid.uuid4())[:4].upper()}"

    balance_response = {}

    if gateway_param == "CASHFREE":
        cashfree_return_url = f"{settings.FRONTEND_URL}/payment-status?merchantTransactionId={merchant_txn_id}&gateway=CASHFREE"
        cf_order = await cashfree_service.create_order(
            order_id=merchant_txn_id,
            amount=payable_amount,
            customer_id=str(current_user.id),
            customer_name=customer_name or "Customer",
            customer_email=customer_email or "noreply@tsboattourism.org",
            customer_phone=user_phone or "9999999999",
            return_url=cashfree_return_url,
        )
        balance_response = {
            "gateway": "CASHFREE",
            "payment_session_id": cf_order.get("payment_session_id"),
            "pg_transaction_id": merchant_txn_id,
            "amount": int(payable_amount * 100),
            "currency": "INR",
            "mode": cashfree_service.env.lower(),
        }
    else:
        callback_url = f"{protocol}://{host}/api/v1/payments/webhook/phonepe"
        redirect_url = f"{settings.FRONTEND_URL}/payment-status?merchantTransactionId={merchant_txn_id}&gateway=PHONEPE"
        phonepe_order = await phonepe_service.create_payment_url(
            amount=payable_amount,
            transaction_id=merchant_txn_id,
            user_id=str(current_user.id),
            redirect_url=redirect_url,
            callback_url=callback_url,
            phone_number=user_phone
        )
        balance_response = {
            "gateway": "PHONEPE",
            "redirect_url": phonepe_order.get("redirect_url"),
            "pg_transaction_id": merchant_txn_id,
            "amount": phonepe_order.get("amount"),
            "currency": "INR",
        }

    # Create CREATED payment ledger row to track this attempt
    payment = Payment(
        booking_id=booking.id,
        payment_reference_id=merchant_txn_id,
        pg_order_id=merchant_txn_id,
        amount=Decimal(str(payable_amount)),
        status=PaymentStatus.CREATED,
        payment_method=gateway_param,
        collected_by_type=gateway_param,
    )
    db.add(payment)
    await db.commit()

    return {
        "status": "success",
        "checkout_data": {
            "booking_public_id": booking.public_id,
            **balance_response,
        }
    }
