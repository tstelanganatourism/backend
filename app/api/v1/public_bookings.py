from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, Response, BackgroundTasks
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
    """Decrypt an Aadhaar and return full decrypted value."""
    try:
        crypto = AadharCryptography()
        return crypto.decrypt(encrypted_value)
    except Exception:
        return encrypted_value

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
    include_food_option: Optional[bool] = False

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
    has_food_addon: Optional[bool] = False
    extra_ids: Optional[List[int]] = None
    selected_extras: Optional[List] = None

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
    background_tasks: BackgroundTasks,
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
    from app.models.enums import AccountStatus, UserRole
    
    is_admin = current_user is not None and current_user.role == UserRole.ADMIN
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
    has_food_addon = request.has_food_addon or False
    
    subtotal_amount = Decimal("0.00")
    room_variant_id = None
    room_obj = None
    required_rooms = 1
    package_variant_id = None
    parent_package = None
    _room_stay_dates = []  # Populated for room bookings with multi-day stays
    target_id = None
    transport_options_to_broadcast = []
    
    # Start inventory validation scope under SELECT FOR UPDATE
    commissionable_base = Decimal("0.00")

    if request.target_type == 'package':
        from app.core.timezone import get_ist_now
        now_ist = get_ist_now()
        today = now_ist.date()
        is_after_6am = now_ist.hour >= 6
        
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
            if is_admin:
                inventory = PackageVariantInventory(
                    variant_id=request.variant_id,
                    date=request.travel_date,
                    total_capacity=request.quantity,
                    booked_count=0,
                    reserved_count=0,
                    is_closed=False,
                )
                db.add(inventory)
                await db.flush()
            else:
                raise HTTPException(status_code=404, detail="Inventory not found for this date")
            
        if inventory.is_closed and not is_admin:
            raise HTTPException(status_code=400, detail="Booking closed for this date")
            
        available = inventory.total_capacity - (inventory.booked_count + inventory.reserved_count)
        if available < request.quantity and not is_admin:
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
        target_id = parent_package.id
        
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
        if total_passengers < min_pax and not is_admin:
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
                    t_type_str = t_opt.type.value if hasattr(t_opt.type, 'value') else str(t_opt.type)
                    default_avail = 9999 if is_admin else (10 if t_type_str == 'SEPARATE_VEHICLE' else max(1, t_opt.capacity or 50))
                    inv_row = PackageTransportInventory(
                        transport_option_id=sel.option_id,
                        date=travel_date_obj,
                        available_count=default_avail,
                        booked_count=0,
                        is_closed=False,
                    )
                    db.add(inv_row)
                    await db.flush()

                if inv_row.is_closed and not is_admin:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Transport option '{t_opt.title}' is closed for {travel_date_obj}."
                    )

                t_type_str = t_opt.type.value if hasattr(t_opt.type, 'value') else str(t_opt.type)
                if t_type_str == 'SEPARATE_VEHICLE':
                    remaining = inv_row.available_count - inv_row.booked_count
                    if sel.quantity > remaining and not is_admin:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Not enough '{t_opt.title}' vehicles available on {travel_date_obj}. Requested: {sel.quantity}, Available: {remaining}."
                        )
                    inv_row.booked_count += sel.quantity
                else:  # SHARED — consume passenger seats
                    seats_needed = student_count if is_student_pkg else (adult_count + child_count)
                    total_seats = inv_row.available_count * (t_opt.capacity or 1)
                    remaining = total_seats - inv_row.booked_count
                    if seats_needed > remaining and not is_admin:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Not enough seats on '{t_opt.title}' for {travel_date_obj}. Needed: {seats_needed}, Remaining: {remaining}."
                        )
                    inv_row.booked_count += seats_needed
                transport_options_to_broadcast.append(sel.option_id)
            # ── End Transport Inventory Check ────────────────────────────────

        refreshment_subtotal = Decimal("0.00")
        if parent_package.has_refreshments and request.include_refreshments:
            total_passengers = student_count if is_student_pkg else (adult_count + child_count)
            min_pass = parent_package.refreshments_min_passengers or 1
            if total_passengers < min_pass and not is_admin:
                raise HTTPException(
                    status_code=400,
                    detail=f"Refreshment rooms (stay/rest option) require a minimum of {min_pass} passengers to book."
                )
            if is_student_pkg:
                r_student = getattr(parent_package, 'refreshment_student_price', None) or \
                            parent_package.refreshment_adult_price or Decimal("0.00")
                refreshment_subtotal = Decimal(str(student_count)) * Decimal(str(r_student))
            else:
                r_adult = parent_package.refreshment_adult_price or Decimal("0.00")
                r_child = parent_package.refreshment_child_price or Decimal("0.00")
                refreshment_subtotal = Decimal(str(adult_count)) * r_adult + Decimal(str(child_count)) * r_child

        food_subtotal = Decimal("0.00")
        if parent_package.has_food_option and (request.include_food_option or request.has_food_addon):
            if is_student_pkg:
                f_student = getattr(parent_package, 'food_student_price', None) or Decimal("0.00")
                food_subtotal = Decimal(str(student_count)) * Decimal(str(f_student))
            else:
                f_adult = parent_package.food_adult_price or Decimal("0.00")
                f_child = parent_package.food_child_price or Decimal("0.00")
                food_subtotal = Decimal(str(adult_count)) * f_adult + Decimal(str(child_count)) * f_child

        # Extras calculation
        extras_subtotal = Decimal("0.00")
        selected_extras_items = []
        raw_extras = getattr(request, 'extra_ids', None) or getattr(request, 'selected_extras', None) or []
        if raw_extras and isinstance(raw_extras, list):
            ext_ids = []
            for item in raw_extras:
                if isinstance(item, int):
                    ext_ids.append(item)
                elif isinstance(item, dict) and 'id' in item:
                    ext_ids.append(item['id'])
            if ext_ids:
                from app.models.package import PackageExtra
                ext_res = await db.execute(select(PackageExtra).where(PackageExtra.id.in_(ext_ids)))
                ext_list = ext_res.scalars().all()
                for ext in ext_list:
                    if is_student_pkg:
                        ext_cost = Decimal(str(student_count)) * Decimal(str(ext.student_price or ext.adult_price or Decimal("0.00")))
                    else:
                        ext_cost = Decimal(str(adult_count)) * Decimal(str(ext.adult_price or Decimal("0.00"))) + Decimal(str(child_count)) * Decimal(str(ext.child_price or Decimal("0.00")))
                    extras_subtotal += ext_cost
                    selected_extras_items.append({
                        "id": ext.id,
                        "title": ext.title,
                        "description": ext.description,
                        "item_total": float(ext_cost)
                    })

        subtotal_amount = base_subtotal + transport_subtotal + refreshment_subtotal + food_subtotal + extras_subtotal
        
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
        from sqlalchemy.orm import joinedload
        if request.room_variant_id:
            variant_query = (
                select(RoomVariant)
                .join(Room, Room.id == RoomVariant.room_id)
                .options(joinedload(RoomVariant.room))
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
                .options(joinedload(RoomVariant.room))
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
        room_obj = variant.room
        target_id = room_obj.id

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

            if inv.is_closed and not is_admin:
                raise HTTPException(
                    status_code=400,
                    detail=f"Rooms unavailable on {stay_date.isoformat()}"
                )

            available = inv.total_rooms - (inv.booked_rooms + inv.reserved_rooms)
            if available < required_rooms and not is_admin:
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
                day_price = inv.weekend_price if inv.weekend_price is not None else variant.weekend_price
            else:
                day_price = inv.weekday_price if inv.weekday_price is not None else variant.weekday_price
            
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

        # Check package-specific commission override if target is package
        comm_type = None
        comm_pct = None
        comm_fixed = None
        if request.target_type == 'package' and 'quota' in locals() and quota:
            comm_type = quota.commission_type
            comm_pct = quota.commission_percentage
            comm_fixed = quota.commission_fixed_amount

        commission_type = comm_type or getattr(current_user, 'commission_type', 'PERCENTAGE') or 'PERCENTAGE'
        
        if comm_pct is not None:
            commission_percentage = Decimal(str(comm_pct))
        else:
            commission_percentage = Decimal(str(current_user.commission_percentage or 0))
            
        if comm_fixed is not None:
            fixed_amount = Decimal(str(comm_fixed))
        else:
            fixed_amount = Decimal(str(current_user.commission_fixed_amount or 0))
        
        if commission_type == 'FIXED_AMOUNT':
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
        "food_subtotal": str(food_subtotal) if 'food_subtotal' in locals() else "0.00",
        "has_food_addon": getattr(request, 'include_food_option', False) or getattr(request, 'has_food_addon', False),
        "extras_amount": str(extras_subtotal) if 'extras_subtotal' in locals() else "0.00",
        "selected_extras": selected_extras_items if 'selected_extras_items' in locals() else [],
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
            "commission_type": commission_type,
            "commission_percentage": str(commission_percentage),
            "commission_fixed_amount": str(fixed_amount) if commission_type == 'FIXED_AMOUNT' else None,
        }
        
    if request.target_type == 'room':
        if request.slot_start:
            pricing_snapshot["slot_start"] = str(request.slot_start)
        if request.slot_end:
            pricing_snapshot["slot_end"] = str(request.slot_end)
        
        first_inv = locked_inventories[0] if 'locked_inventories' in locals() and locked_inventories else None
        h_name = (first_inv.hotel_name if first_inv and first_inv.hotel_name else None) or (room_obj.lodge_name if room_obj else None)
        h_addr = (first_inv.hotel_address if first_inv and first_inv.hotel_address else None) or (room_obj.address if room_obj else None)
        h_map = (first_inv.hotel_map_url if first_inv and first_inv.hotel_map_url else None) or (room_obj.map_url if room_obj else None)
        
        if h_name:
            pricing_snapshot["hotel_name"] = h_name
        if h_addr:
            pricing_snapshot["hotel_address"] = h_addr
        if h_map:
            pricing_snapshot["hotel_map_url"] = h_map
        
    # --- Payment Gateway Order Generation ---
    payment_percentage = request.payment_percentage if request.payment_percentage is not None else 100.0
    
    if request.target_type == 'room' and room_obj:
        adv_type = room_obj.advance_payment_type
        adv_value = room_obj.advance_payment_value
        adv_type_str = adv_type.value if hasattr(adv_type, 'value') else str(adv_type)
        
        if adv_type_str == 'FULL_PAYMENT':
            min_percentage = 100.0
        elif adv_type_str == 'PERCENTAGE':
            min_percentage = float(adv_value) if adv_value else 50.0
        elif adv_type_str == 'FIXED_AMOUNT':
            # Calculate per room: fixed_amount * required_rooms
            fixed_amt_total = Decimal(str(adv_value)) * Decimal(str(required_rooms or 1))
            if total_amount > 0:
                pct = (fixed_amt_total / total_amount) * Decimal("100")
                min_percentage = float(pct.quantize(Decimal("0.01")))
                min_percentage = min(100.0, max(0.0, min_percentage))
            else:
                min_percentage = 100.0
        else:
            min_percentage = 100.0
            
        payment_percentage = max(min_percentage, payment_percentage)
        
    elif request.target_type == 'package' and parent_package:
        adv_type = parent_package.advance_payment_type
        adv_value = parent_package.advance_payment_value
        adv_type_str = adv_type.value if hasattr(adv_type, 'value') else str(adv_type)
        
        if adv_type_str == 'FULL_PAYMENT':
            min_percentage = 100.0
        elif adv_type_str == 'PERCENTAGE':
            min_percentage = float(adv_value) if adv_value else 50.0
        elif adv_type_str == 'FIXED_AMOUNT':
            # Calculate for total booking, not per passenger
            fixed_amt_total = Decimal(str(adv_value))
            if total_amount > 0:
                pct = (fixed_amt_total / total_amount) * Decimal("100")
                min_percentage = float(pct.quantize(Decimal("0.01")))
                min_percentage = min(100.0, max(0.0, min_percentage))
            else:
                min_percentage = 100.0
        else:
            min_percentage = 100.0
            
        payment_percentage = max(min_percentage, payment_percentage)
    else:
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



    pricing_snapshot["payment_percentage"] = str(payment_percentage)
    pricing_snapshot["tourist_amount_payable"] = str(tourist_amount_payable)
    pricing_snapshot["actual_paid_advance"] = str(payable_amount)

    if request.expected_amount is not None:
        pass  # Frontend sends expected_amount but we process checkout with realtime calculated price.

    # ═══════════════════════════════════════════════════════════════════════════
    # AGENT DIRECT BOOKING PATH
    # ═══════════════════════════════════════════════════════════════════════════
    # When the logged-in user is an AGENT, we skip the payment gateway entirely.
    # The agent's commission is automatically applied as their "payment".
    # The booking is created immediately with PARTIAL_PAID status:
    #   - paid_amount    = agent_commission (agent's share — already settled)
    #   - remaining_balance = agent_payable (tourist pays this at the office)
    # Ticket is generated and SMS is sent immediately.
    # ═══════════════════════════════════════════════════════════════════════════
    if is_agent:
        from app.models.booking import generate_pnr_prefix
        from app.models.package import Package, PackageVariant as PV
        from app.models.enums import PackageType
        from app.models.payment import Payment
        from app.models.enums import PaymentStatus
        from app.core.security import AadharCryptography, AadharHashing
        from sqlalchemy import text as sa_text
        from app.models.room import Room as RoomModel

        now = get_ist_now()

        # ── 1. Generate PNR / public_id ──────────────────────────────────────
        if request.target_type == 'room':
            seq_res = await db.execute(sa_text("SELECT nextval('booking_seq_ac')"))
            seq_val = seq_res.scalar()
            r_title = room_obj.lodge_name if room_obj else "ROOM"
            prefix = generate_pnr_prefix(r_title)
            date_str = request.travel_date.strftime("%d%m%Y")
            public_id_val = f"TSBOAT_{prefix}_{date_str}_{seq_val:04d}"
        else:
            pkg_type_res = await db.execute(
                select(Package.type, Package.title)
                .join(PV, PV.package_id == Package.id)
                .where(PV.id == package_variant_id)
            )
            pkg_row = pkg_type_res.first()
            pkg_type_val = pkg_row[0] if pkg_row else None
            pkg_title_val = pkg_row[1] if pkg_row else "PACKAGE"
            prefix = generate_pnr_prefix(pkg_title_val)
            date_str = request.travel_date.strftime("%d%m%Y")
            if pkg_type_val == PackageType.TRIP:
                seq_res = await db.execute(sa_text("SELECT nextval('booking_seq_ss')"))
            else:
                seq_res = await db.execute(sa_text("SELECT nextval('booking_seq_bt')"))
            seq_val = seq_res.scalar()
            public_id_val = f"TSBOAT_{prefix}_{date_str}_{seq_val:04d}"

        # ── 2. Compute booking financials ────────────────────────────────────
        # agent_discount  = commission the agent earns (their share)
        # agent_payable   = what the tourist owes at the office
        # paid_amount     = agent_discount (commission already "paid" via agency agreement)
        # remaining_balance = agent_payable
        paid_amount_agent = agent_discount  # commission = agent's settled portion
        remaining_balance_agent = agent_payable  # tourist pays this at office

        # Determine booking status based on whether commission covers full amount
        if remaining_balance_agent <= Decimal("0.01"):
            booking_status_agent = BookingStatus.FULLY_PAID
            remaining_balance_agent = Decimal("0.00")
        else:
            booking_status_agent = BookingStatus.PARTIAL_PAID

        # Add agent booking metadata to pricing snapshot
        pricing_snapshot["payment_method"] = "AGENT_COMMISSION"
        pricing_snapshot["agent_direct_booking"] = True
        pricing_snapshot["tourist_due_at_office"] = str(agent_payable)
        pricing_snapshot["payment_percentage"] = "100"
        pricing_snapshot["tourist_amount_payable"] = str(total_amount)
        pricing_snapshot["actual_paid_advance"] = str(paid_amount_agent)

        # ── 3. Convert reserved inventory → booked ──────────────────────────
        if request.target_type == 'package':
            inventory.reserved_count = max(0, inventory.reserved_count - request.quantity)
            inventory.booked_count += request.quantity
        else:  # room
            for inv_item in locked_inventories:
                inv_item.reserved_rooms = max(0, inv_item.reserved_rooms - required_rooms)
                inv_item.booked_rooms += required_rooms

        # ── 4. Create Booking ────────────────────────────────────────────────
        booking = Booking(
            public_id=public_id_val,
            user_id=current_user.id,
            agent_id=current_user.id,
            source=BookingSource.AGENT,
            customer_email=request.customer_email or current_user.email,
            variant_id=package_variant_id,
            room_variant_id=room_variant_id,
            travel_date=request.travel_date,
            adult_count=adult_count if 'adult_count' in locals() else request.quantity,
            child_count=child_count if 'child_count' in locals() else 0,
            student_count=student_count if 'student_count' in locals() else 0,
            has_refreshment_addon=bool(pricing_snapshot.get('has_refreshment_addon', False)),
            has_food_addon=bool(pricing_snapshot.get('has_food_addon', False)),
            subtotal_amount=subtotal_amount,
            coupon_discount=coupon_discount,
            coupon_applied=coupon_applied,
            gst_amount=gst_amount,
            gateway_fee=gateway_fee,
            total_amount=total_amount,
            paid_amount=paid_amount_agent,
            remaining_balance=remaining_balance_agent,
            agent_commission=agent_discount,
            status=booking_status_agent,
            pricing_snapshot=pricing_snapshot,
        )
        db.add(booking)
        await db.flush()

        # ── 5. Persist Passengers ────────────────────────────────────────────
        crypto = AadharCryptography()
        if request.passengers:
            for p in request.passengers:
                gender_enum = None
                if p.gender:
                    try:
                        from app.models.enums import GenderType
                        gender_enum = GenderType(p.gender.upper())
                    except (ValueError, KeyError):
                        pass
                raw_aadhaar = (p.aadhaar or '').strip()
                encrypted_aadhaar = crypto.encrypt(raw_aadhaar) if raw_aadhaar else None
                hashed_aadhaar = AadharHashing.hash_aadhar(raw_aadhaar) if raw_aadhaar else None
                db.add(BookingPassenger(
                    booking_id=booking.id,
                    full_name=p.full_name,
                    age=p.age or 0,
                    gender=gender_enum,
                    phone_number=p.phone,
                    relationship_to_lead=p.relationship,
                    is_primary=bool(p.is_primary),
                    aadhar_encrypted=encrypted_aadhaar,
                    aadhar_hash=hashed_aadhaar,
                    student_class=p.student_class or None,
                ))

        # ── 5b. Persist Stay Dates (if room booking) ─────────────────────────
        if request.target_type == 'room' and '_room_stay_dates' in locals() and _room_stay_dates:
            for sd in _room_stay_dates:
                db.add(BookingStayDate(booking_id=booking.id, date=sd))

        # ── 5c. Payment ledger row (AGENT_COMMISSION — no gateway) ───────────
        payment_ref = f"AGENT_{public_id_val}_{uuid.uuid4().hex[:8].upper()}"
        db.add(Payment(
            booking_id=booking.id,
            payment_reference_id=payment_ref,
            pg_order_id=None,
            pg_payment_id=None,
            amount=paid_amount_agent,
            status=PaymentStatus.CAPTURED,
            payment_method="AGENT_COMMISSION",
            collected_by_type="AGENT_COMMISSION",
        ))

        # ── 5d. Coupon usage increment ────────────────────────────────────────
        if coupon_applied:
            try:
                coupon_obj = await db.scalar(
                    select(Coupon).where(Coupon.code == coupon_applied)
                )
                if coupon_obj:
                    coupon_obj.usage_count = (coupon_obj.usage_count or 0) + 1
            except Exception:
                pass

        await db.flush()

        # ── 6. SSE Inventory Update ──────────────────────────────────────────
        import time
        from app.utils.sse import sse_manager, broadcast_transport_update

        if request.target_type.lower() == 'package':
            sse_payload = {
                "version": int(time.time() * 1000),
                "timestamp": now.isoformat(),
                "package_id": variant.package_id,
                "travel_date": str(request.travel_date),
                "available": inventory.total_capacity - (inventory.booked_count + inventory.reserved_count),
                "reserved": inventory.reserved_count,
                "booked": inventory.booked_count,
                "is_closed": inventory.is_closed,
                "variant_id": package_variant_id,
            }
        else:
            sse_payload = None

        await db.commit()

        if request.target_type.lower() == 'package':
            for opt_id in transport_options_to_broadcast:
                await broadcast_transport_update(db, opt_id, request.travel_date)
            if sse_payload:
                await sse_manager.broadcast_event("package", str(variant.package_id), "INVENTORY_UPDATE", sse_payload)

        # ── 7. Trigger ticket/email/SMS generation ───────────────────────────
        async def _agent_booking_documents(b_id: int, b_public_id: str):
            try:
                from app.services.pdf_generator import process_post_booking_documents_task
                await process_post_booking_documents_task(None, b_id, is_fully_paid=(booking_status_agent == BookingStatus.FULLY_PAID))
                logger.info(f"Agent booking documents generated for {b_public_id}")
            except Exception as doc_err:
                logger.error(f"Agent booking document generation failed for {b_public_id}: {doc_err}")

            try:
                async with __import__('app.db.session', fromlist=['AsyncSessionLocal']).AsyncSessionLocal() as sms_db:
                    from app.services.sms_service import get_booking_sms_payload
                    from app.worker import get_arq_pool
                    sms_payload = await get_booking_sms_payload(b_id, sms_db)
                    if sms_payload:
                        arq_pool = await get_arq_pool()
                        if arq_pool:
                            await arq_pool.enqueue_job("dispatch_sms_payload", sms_payload)
            except Exception as sms_err:
                logger.warning(f"Agent booking SMS enqueue failed for {b_public_id}: {sms_err}")

        background_tasks.add_task(_agent_booking_documents, booking.id, booking.public_id)

        logger.info(
            f"Agent direct booking confirmed | booking={public_id_val} | agent={current_user.id} "
            f"| commission={agent_discount} | tourist_due={agent_payable}"
        )

        return {
            "status": "success",
            "booking_confirmed": True,
            "payment_method": "AGENT_COMMISSION",
            "message": "Booking confirmed via agent commission. Tourist pays the remaining amount at the office.",
            "checkout_data": {
                "booking_id": booking.public_id,
                "booking_status": booking_status_agent.value,
                "total_amount": float(total_amount),
                "agent_commission": float(agent_discount),
                "tourist_due_at_office": float(agent_payable),
            }
        }
    # ═══════════════════════════════════════════════════════════════════════════
    # END AGENT DIRECT BOOKING PATH
    # ═══════════════════════════════════════════════════════════════════════════

    draft_id = "DRF-" + str(uuid.uuid4())[:8].upper()
    merchant_txn_id = f"TXN_{draft_id}_{str(uuid.uuid4())[:4].upper()}"

    host = fastapi_req.headers.get('host')
    protocol = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"

    # user_phone and user_email extracted above

    # Determine selected gateway (strictly PhonePe)
    selected_gateway = "PHONEPE"

    # --- Gateway-specific order creation ---
    callback_url = f"{protocol}://{host}/api/v1/payments/webhook/phonepe"
    referer = fastapi_req.headers.get("referer")
    origin_url = settings.FRONTEND_URL
    if referer:
        from urllib.parse import urlparse
        parsed = urlparse(referer)
        origin_url = f"{parsed.scheme}://{parsed.netloc}"
    redirect_url = f"{origin_url}/payment-status?merchantTransactionId={merchant_txn_id}&gateway=PHONEPE"
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

    # Log Checkout Funnel Activity and Dispatch Admin Email Alert
    try:
        from app.models.activity_log import CheckoutFunnelLog
        from app.services.abandoned_lead_service import send_admin_abandoned_lead_notification

        header_sess = fastapi_req.headers.get("x-session-id")
        session_id = header_sess or f"sess_{draft_id}"
        
        target_title_val = "Tour Package"
        if request.target_type.lower() == 'package' and 'parent_package' in locals() and parent_package:
            target_title_val = parent_package.title
        elif request.target_type.lower() == 'room' and 'room_obj' in locals() and room_obj:
            target_title_val = room_obj.lodge_name

        variant_title_val = None
        if request.target_type.lower() == 'package' and 'variant' in locals() and variant:
            variant_title_val = variant.variant_name

        funnel_log = CheckoutFunnelLog(
            session_id=session_id,
            user_id=current_user.id if current_user else None,
            funnel_stage="CHECKOUT_INITIATED",
            target_type=request.target_type,
            target_id=target_id,
            target_title=target_title_val,
            variant_title=variant_title_val,
            travel_date=str(request.travel_date),
            adult_count=adult_count if 'adult_count' in locals() else request.quantity,
            child_count=child_count if 'child_count' in locals() else 0,
            student_count=student_count if 'student_count' in locals() else 0,
            total_amount=total_amount,
            coupon_code=coupon_applied,
            customer_name=user_name,
            customer_email=user_email,
            customer_phone=user_phone,
            passengers_data=[p.model_dump() for p in request.passengers] if request.passengers else None,
            booking_public_id=draft_id,
            payment_gateway=selected_gateway,
            ip_address=fastapi_req.client.host if fastapi_req.client else None,
            user_agent=fastapi_req.headers.get("user-agent"),
        )
        db.add(funnel_log)
        await db.flush()
        background_tasks.add_task(send_admin_abandoned_lead_notification, funnel_log, None)
    except Exception as f_err:
        logger.warning(f"Backend checkout funnel logging error: {f_err}")

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
    if request.target_type.lower() == 'package':
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
    
    paid_statuses = {BookingStatus.FULLY_PAID, BookingStatus.PARTIAL_PAID}
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
        select(
            Booking,
            Package.title,
            PackageVariant.title,
            Room.lodge_name,
            RoomVariant.variant_name,
            Room.slot_start,
            Room.slot_end,
            Room.address,
            Room.id,
            Package.type,
            Package.cover_image_url,
            Room.cover_image_url,
        )
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
    room_map_url = None
    hotel_name = package_title  # Default to original Lodge Name

    if b.room_variant_id:
        # Resolve dynamic hotel details from room slot inventory
        s_start = None
        s_end = None
        if b.pricing_snapshot:
            s_start = b.pricing_snapshot.get('slot_start')
            s_end = b.pricing_snapshot.get('slot_end')
        
        from app.models.room import RoomSlotInventory
        inv_stmt = select(RoomSlotInventory).where(
            RoomSlotInventory.room_variant_id == b.room_variant_id,
            RoomSlotInventory.date == b.travel_date
        )
        if s_start and s_end:
            from datetime import datetime
            try:
                s_time = datetime.strptime(s_start, "%H:%M:%S").time()
                e_time = datetime.strptime(s_end, "%H:%M:%S").time()
                inv_stmt = inv_stmt.where(
                    RoomSlotInventory.slot_start == s_time,
                    RoomSlotInventory.slot_end == e_time
                )
            except Exception:
                pass
        inv_res = await db.execute(inv_stmt)
        inv_row = inv_res.scalars().first()
        
        # Keep package_title as original Lodge Name
        package_title = row[3] or "Godavari Riverside Bamboo Huts"
        hotel_name = package_title

        if inv_row:
            if inv_row.hotel_name:
                hotel_name = inv_row.hotel_name
            if inv_row.hotel_address:
                room_address = inv_row.hotel_address
            if inv_row.hotel_map_url:
                room_map_url = inv_row.hotel_map_url
        if b.pricing_snapshot:
            if b.pricing_snapshot.get('hotel_name'):
                hotel_name = b.pricing_snapshot.get('hotel_name')
            if b.pricing_snapshot.get('hotel_address'):
                room_address = b.pricing_snapshot.get('hotel_address')
            if b.pricing_snapshot.get('hotel_map_url'):
                room_map_url = b.pricing_snapshot.get('hotel_map_url')
    
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
    meals_list = []
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

        meals_list = []
        from app.models.package import PackageMealItem
        meal_stmt = select(PackageMealItem).where(
            PackageMealItem.package_id == variant.package_id,
            PackageMealItem.deleted_at.is_(None)
        ).order_by(PackageMealItem.sort_order.asc())
        meal_res = await db.execute(meal_stmt)
        for m in meal_res.scalars().all():
            meals_list.append({
                "id": m.id,
                "meal_type": m.meal_type.value if hasattr(m.meal_type, "value") else str(m.meal_type),
                "name": m.name,
                "serving_time": m.serving_time,
                "description": m.description,
                "is_vegetarian": m.is_vegetarian,
                "day_number": m.day_number,
                "sort_order": m.sort_order
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

    if not payment_ledger and float(b.paid_amount or 0) > 0:
        payment_ledger.append({
            "id": 0,
            "amount": float(b.paid_amount),
            "payment_method": "ADMIN_MANUAL" if b.agent_id else "ONLINE",
            "status": "CAPTURED",
            "collected_by_type": "ONLINE",
            "collected_by_label": "Verified Booking Payment",
            "payment_reference_id": f"TXN_{b.public_id}",
            "created_at": b.created_at.isoformat() if b.created_at else None,
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
        "remaining_balance": float(b.remaining_balance),
        "paid_amount": float(b.paid_amount),
        "status": b.status.value if hasattr(b.status, 'value') else str(b.status),
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "package_title": package_title,
        "variant_title": variant_title,
        "hotel_name": hotel_name,
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
        "room_map_url": room_map_url,
        "room_highlights": room_highlights,
        "itinerary": itinerary,
        "meals": meals_list,
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
        "cover_image_url": row[11] if b.room_variant_id else row[10],
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
    Tourist balance checkout — supports PhonePe.
    Generates a payment session for the remaining balance.
    """
    from app.services.phonepe_client import phonepe_service
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
        
    gateway_param = "PHONEPE"

    host = fastapi_req.headers.get('host')
    protocol = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"

    user_phone = None
    if booking.passengers:
        user_phone = next((p.phone_number for p in booking.passengers if p.phone_number), None)

    import uuid
    merchant_txn_id = f"TXN_BAL_{str(uuid.uuid4())[:8].upper()}_{str(uuid.uuid4())[:4].upper()}"

    callback_url = f"{protocol}://{host}/api/v1/payments/webhook/phonepe"
    referer = fastapi_req.headers.get("referer")
    origin_url = settings.FRONTEND_URL
    if referer:
        from urllib.parse import urlparse
        parsed = urlparse(referer)
        origin_url = f"{parsed.scheme}://{parsed.netloc}"
    redirect_url = f"{origin_url}/payment-status?merchantTransactionId={merchant_txn_id}&gateway=PHONEPE"
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

@router.get("/{booking_id}/pdf")
async def download_public_booking_pdf(
    booking_id: str,
    doc_type: str = Query("ticket", enum=["ticket", "invoice", "form"]),
    secret: Optional[str] = None
):
    """
    Directly generate and stream vector PDF for ticket, invoice, or boarding form
    using Playwright headless Chromium matching the exact print layout.
    """
    import hmac
    import hashlib
    SECRET_KEY = "tsaptourismpapikondalubadhrachalam"
    expected = hmac.new(SECRET_KEY.encode("utf-8"), booking_id.encode("utf-8"), hashlib.sha256).hexdigest()
    
    # If secret is passed, verify it; otherwise compute for valid public booking
    if not secret:
        secret = expected
    elif secret != expected and not booking_id.startswith("DEMO-"):
        raise HTTPException(status_code=403, detail="Invalid authorization secret for document PDF download")

    # In production use FRONTEND_URL; locally use 127.0.0.1:3000
    if settings.ENVIRONMENT == "production":
        frontend_base = settings.FRONTEND_URL.rstrip("/")
    else:
        frontend_base = "http://127.0.0.1:3000"
    url = f"{frontend_base}/print/{doc_type}/{booking_id}?secret={secret}"
    from app.services.pdf_generator import sync_generate_pdf
    try:
        pdf_bytes = await asyncio.to_thread(sync_generate_pdf, url)
    except Exception as e:
        logger.error(f"Error generating PDF for {url}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF: {str(e)}")

    filename = f"{doc_type.capitalize()}_{booking_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition"
        }
    )
