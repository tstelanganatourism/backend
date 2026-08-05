from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal
import uuid
import json
from loguru import logger
from sqlalchemy import select, text
from pydantic import BaseModel
from typing import Optional

from app.db.session import get_db
from app.models.booking import Booking, BookingDraft, BookingPassenger, BookingStayDate
from app.models.package import PackageVariantInventory
from app.models.room import RoomSlotInventory
from app.models.coupon import Coupon
from app.models.enums import BookingStatus, BookingSource, GenderType
from app.services.phonepe_client import phonepe_service
from app.core.security import AadharCryptography, AadharHashing
from app.core.timezone import get_ist_now

async def release_draft_inventory(draft: BookingDraft, db: AsyncSession) -> list:
    """
    Releases locked inventory for a single BookingDraft and returns SSE payloads.
    """
    sse_payloads = []
    logger.info(f"Releasing inventory for draft {draft.draft_id} ({draft.target_type})")
    
    try:
        if draft.target_type == 'package':
            inv_query = select(PackageVariantInventory).where(
                PackageVariantInventory.variant_id == draft.variant_id,
                PackageVariantInventory.date == draft.travel_date
            ).with_for_update()
            inv_res = await db.execute(inv_query)
            inventory = inv_res.scalar_one_or_none()
            if inventory:
                inventory.reserved_count = max(0, inventory.reserved_count - draft.quantity)
                await db.flush()
                
                from sqlalchemy.orm import joinedload
                from app.models.package import PackageVariant
                v_res = await db.execute(select(PackageVariant).options(joinedload(PackageVariant.package)).where(PackageVariant.id == draft.variant_id))
                variant = v_res.scalar_one_or_none()
                if variant:
                    from app.utils.sse import build_package_sse_payload
                    sse_payloads.append(build_package_sse_payload(variant, inventory, draft.travel_date))
                    
            # Release transport inventory
            payload = draft.checkout_payload or {}
            effective_selections = payload.get("transport_selections") or []
            if not effective_selections and payload.get("transport_option_id"):
                effective_selections = [{"option_id": payload.get("transport_option_id"), "quantity": 1}]
                
            if effective_selections:
                from app.models.package import PackageTransportInventory, PackageTransportOption
                from app.utils.sse import broadcast_transport_update
                
                selected_opt_ids = [s.get("option_id") if isinstance(s, dict) else s.option_id for s in effective_selections]
                t_opts_res = await db.execute(select(PackageTransportOption).where(PackageTransportOption.id.in_(selected_opt_ids)))
                t_opts_map = {t.id: t for t in t_opts_res.scalars().all()}
                
                for sel in effective_selections:
                    sel_id = sel.get("option_id") if isinstance(sel, dict) else sel.option_id
                    sel_qty = sel.get("quantity") if isinstance(sel, dict) else sel.quantity
                    t_opt = t_opts_map.get(sel_id)
                    if not t_opt:
                        continue
                        
                    inv_row = await db.scalar(
                        select(PackageTransportInventory).where(
                            PackageTransportInventory.transport_option_id == sel_id,
                            PackageTransportInventory.date == draft.travel_date,
                            PackageTransportInventory.deleted_at.is_(None)
                        ).with_for_update()
                    )
                    if inv_row:
                        t_type_str = t_opt.type.value if hasattr(t_opt.type, 'value') else str(t_opt.type)
                        if t_type_str == 'SEPARATE_VEHICLE':
                            inv_row.booked_count = max(0, inv_row.booked_count - sel_qty)
                        else:
                            is_student_pkg = payload.get("student_count") is not None and payload.get("student_count") > 0
                            adult_count = payload.get("adult_count") or draft.quantity
                            child_count = payload.get("child_count") or 0
                            student_count = payload.get("student_count") or 0
                            seats_needed = student_count if is_student_pkg else (adult_count + child_count)
                            inv_row.booked_count = max(0, inv_row.booked_count - seats_needed)
                        
                        await db.flush()
                        await broadcast_transport_update(db, sel_id, draft.travel_date)

        elif draft.target_type == 'room':
            payload = draft.checkout_payload or {}
            from datetime import date, timedelta, time
            
            travel_date_str = payload.get('travel_date') or str(draft.travel_date)
            arrival = date.fromisoformat(travel_date_str)
            departure_str = payload.get('departure_date')
            departure = date.fromisoformat(departure_str) if departure_str else (arrival + timedelta(days=1))
            
            current = arrival
            stay_dates = []
            while current < departure:
                stay_dates.append(current)
                current += timedelta(days=1)
                
            slot_start = time.fromisoformat(payload['slot_start']) if payload.get('slot_start') else None
            slot_end = time.fromisoformat(payload['slot_end']) if payload.get('slot_end') else None

            from app.models.room import RoomVariant
            room_var_id = draft.room_variant_id or payload.get('room_variant_id')
            if room_var_id:
                room_var = await db.execute(select(RoomVariant).where(RoomVariant.id == room_var_id))
                rv = room_var.scalar_one_or_none()
                if rv:
                    from app.services.room_calculation import calculate_required_rooms
                    required_rooms = calculate_required_rooms(draft.quantity, rv.capacity_per_room)

                    for stay_date in stay_dates:
                        inv_query = select(RoomSlotInventory).where(
                            RoomSlotInventory.room_variant_id == room_var_id,
                            RoomSlotInventory.date == stay_date,
                            RoomSlotInventory.slot_start == slot_start,
                            RoomSlotInventory.slot_end == slot_end
                        ).with_for_update()
                        inv_res = await db.execute(inv_query)
                        room_inv = inv_res.scalar_one_or_none()
                        if room_inv:
                            room_inv.reserved_rooms = max(0, room_inv.reserved_rooms - required_rooms)
                            
    except Exception as e:
        logger.error(f"Failed to release draft inventory for {draft.draft_id}: {e}")
        
    return sse_payloads

router = APIRouter()


async def _finalize_draft(
    draft: BookingDraft,
    payment_id: str,
    db: AsyncSession,
    background_tasks: BackgroundTasks | None = None,
    sse_payloads: list = None,
    payment_source: str = "PHONEPE"
) -> str:
    """
    Idempotent function to convert BookingDraft to Booking.
    Returns the public_id of the generated Booking.
    Works for both PhonePe and Cashfree.
    """
    # 1. Double check if a booking already exists for this transaction
    existing_booking = await db.execute(
        select(Booking).where(Booking.pricing_snapshot['pg_transaction_id'].astext == draft.pg_transaction_id)
    )
    existing = existing_booking.scalar_one_or_none()
    if existing:
        logger.info(f"Booking already finalized for transaction {draft.pg_transaction_id}")
        return existing.public_id

    # 2. Promote reserved_count → booked_count in inventory
    if draft.target_type == 'package':
        inv_query = select(PackageVariantInventory).where(
            PackageVariantInventory.variant_id == draft.variant_id,
            PackageVariantInventory.date == draft.travel_date
        ).with_for_update()
        inv_res = await db.execute(inv_query)
        inventory = inv_res.scalar_one_or_none()
        if inventory:
            inventory.reserved_count = max(0, inventory.reserved_count - draft.quantity)
            inventory.booked_count += draft.quantity

            await db.flush()
            if sse_payloads is not None:
                from sqlalchemy.orm import joinedload
                from app.models.package import PackageVariant
                v_res = await db.execute(select(PackageVariant).options(joinedload(PackageVariant.package)).where(PackageVariant.id == draft.variant_id))
                variant = v_res.scalar_one_or_none()
                if variant:
                    from app.utils.sse import build_package_sse_payload
                    sse_payloads.append(build_package_sse_payload(variant, inventory, draft.travel_date))

    elif draft.target_type == 'room':
        payload = draft.checkout_payload
        from datetime import date, timedelta, time
        arrival = date.fromisoformat(payload['travel_date'])
        departure_str = payload.get('departure_date')
        departure = date.fromisoformat(departure_str) if departure_str else (arrival + timedelta(days=1))

        current = arrival
        stay_dates = []
        while current < departure:
            stay_dates.append(current)
            current += timedelta(days=1)

        slot_start = time.fromisoformat(payload['slot_start']) if payload.get('slot_start') else None
        slot_end = time.fromisoformat(payload['slot_end']) if payload.get('slot_end') else None

        from app.models.room import RoomVariant
        room_var = await db.execute(select(RoomVariant).where(RoomVariant.id == draft.room_variant_id))
        rv = room_var.scalar_one()
        from app.services.room_calculation import calculate_required_rooms
        required_rooms = calculate_required_rooms(draft.quantity, rv.capacity_per_room)

        for stay_date in stay_dates:
            inv_query = select(RoomSlotInventory).where(
                RoomSlotInventory.room_variant_id == draft.room_variant_id,
                RoomSlotInventory.date == stay_date,
                RoomSlotInventory.slot_start == slot_start,
                RoomSlotInventory.slot_end == slot_end
            ).with_for_update()
            inv_res = await db.execute(inv_query)
            inv = inv_res.scalar_one_or_none()
            if inv:
                inv.reserved_rooms = max(0, inv.reserved_rooms - required_rooms)
                inv.booked_rooms += required_rooms
                
                if sse_payloads is not None:
                    import time
                    from app.core.timezone import get_ist_now
                    sse_payloads.append({
                        "version": int(time.time() * 1000),
                        "timestamp": get_ist_now().isoformat(),
                        "room_id": rv.room_id,
                        "travel_date": str(stay_date),
                        "available": inv.total_rooms - (inv.booked_rooms + inv.reserved_rooms),
                        "reserved": inv.reserved_rooms,
                        "booked": inv.booked_rooms,
                        "is_closed": inv.is_closed,
                        "variant_id": inv.room_variant_id,
                        "slot_start": str(inv.slot_start),
                        "slot_end": str(inv.slot_end)
                    })

    # 3. Prepare pricing snapshot
    snapshot = draft.pricing_snapshot
    snapshot["pg_transaction_id"] = draft.pg_transaction_id
    snapshot["pg_payment_id"] = payment_id
    snapshot["payment_gateway"] = payment_source
    if draft.target_type == 'room':
        snapshot["slot_start"] = draft.checkout_payload.get('slot_start')
        snapshot["slot_end"] = draft.checkout_payload.get('slot_end')
        if 'inv' in locals() and inv:
            if inv.hotel_name and "hotel_name" not in snapshot:
                snapshot["hotel_name"] = inv.hotel_name
            if inv.hotel_address and "hotel_address" not in snapshot:
                snapshot["hotel_address"] = inv.hotel_address
            if inv.hotel_map_url and "hotel_map_url" not in snapshot:
                snapshot["hotel_map_url"] = inv.hotel_map_url

    # 4. Increment coupon usage
    if draft.coupon_applied:
        coupon_query = select(Coupon).where(Coupon.code == draft.coupon_applied).with_for_update()
        c_res = await db.execute(coupon_query)
        coupon = c_res.scalar_one_or_none()
        if coupon:
            coupon.usage_count += 1

    # 5. Calculate payment status
    tourist_total = Decimal(snapshot['tourist_total'])
    tourist_amount_payable = Decimal(snapshot.get('tourist_amount_payable', str(draft.amount_payable)))

    tourist_remaining = (tourist_total - tourist_amount_payable).quantize(Decimal("0.01"))
    tourist_remaining = max(Decimal("0.00"), tourist_remaining)

    if tourist_remaining <= Decimal("0.01"):
        booking_status = BookingStatus.FULLY_PAID
        tourist_remaining = Decimal("0.00")
    else:
        booking_status = BookingStatus.PARTIAL_PAID

    # Determine prefix and sequence
    public_id = ""
    if draft.target_type == 'room':
        seq_res = await db.execute(text("SELECT nextval('booking_seq_ac')"))
        seq_val = seq_res.scalar()
        
        # Get room title prefix
        from app.models.room import RoomVariant, Room
        from app.models.booking import generate_pnr_prefix
        room_res = await db.execute(
            select(Room.lodge_name)
            .join(RoomVariant, RoomVariant.room_id == Room.id)
            .where(RoomVariant.id == draft.room_variant_id)
        )
        room_title = room_res.scalar_one_or_none() or "ROOM"
        prefix = generate_pnr_prefix(room_title)
        
        date_str = draft.travel_date.strftime("%d%m%Y")
        seq_str = f"{seq_val:04d}"
        public_id = f"TSBOAT_{prefix}_{date_str}_{seq_str}"
    else:
        from app.models.package import PackageVariant, Package
        from app.models.enums import PackageType
        from app.models.booking import generate_pnr_prefix
        pkg_res = await db.execute(
            select(Package.type, Package.title)
            .join(PackageVariant, PackageVariant.package_id == Package.id)
            .where(PackageVariant.id == draft.variant_id)
        )
        pkg_row = pkg_res.first()
        pkg_type = pkg_row[0] if pkg_row else None
        pkg_title = pkg_row[1] if pkg_row else "PACKAGE"
        
        prefix = generate_pnr_prefix(pkg_title)
        date_str = draft.travel_date.strftime("%d%m%Y")

        if pkg_type == PackageType.TRIP:
            seq_res = await db.execute(text("SELECT nextval('booking_seq_ss')"))
        else:
            seq_res = await db.execute(text("SELECT nextval('booking_seq_bt')"))
            
        seq_val = seq_res.scalar()
        seq_str = f"{seq_val:04d}"
        public_id = f"TSBOAT_{prefix}_{date_str}_{seq_str}"

    # 6. Generate actual Booking
    booking = Booking(
        public_id=public_id,
        user_id=draft.user_id,
        agent_id=draft.agent_id,
        source=BookingSource.AGENT if draft.agent_id else BookingSource.PUBLIC,
        customer_email=draft.checkout_payload.get('customer_email'),
        variant_id=draft.variant_id,
        room_variant_id=draft.room_variant_id,
        travel_date=draft.travel_date,
        adult_count=draft.checkout_payload.get('adult_count') or draft.quantity,
        child_count=draft.checkout_payload.get('child_count') or 0,
        student_count=snapshot.get('student_count') or draft.checkout_payload.get('student_count') or 0,
        has_refreshment_addon=bool(snapshot.get('has_refreshment_addon', False)),
        has_food_addon=bool(snapshot.get('has_food_addon', False)),
        subtotal_amount=Decimal(snapshot['subtotal_amount']),
        coupon_discount=Decimal(snapshot['coupon_discount']),
        coupon_applied=draft.coupon_applied,
        gst_amount=Decimal(snapshot['gst_amount']),
        gateway_fee=Decimal(snapshot['gateway_fee']),
        total_amount=tourist_total,
        paid_amount=tourist_amount_payable,
        remaining_balance=tourist_remaining,
        agent_commission=Decimal(snapshot.get('agent_discount', "0.00")),
        status=booking_status,
        pricing_snapshot=snapshot
    )
    db.add(booking)
    await db.flush()

    # 5. Persist Passengers
    passengers_payload = draft.checkout_payload.get('passengers', [])
    crypto = AadharCryptography()
    for p in passengers_payload:
        gender_enum = None
        if p.get('gender'):
            try:
                gender_enum = GenderType(p['gender'].upper())
            except (ValueError, KeyError):
                pass

        raw_aadhaar = (p.get('aadhaar') or '').strip()
        encrypted_aadhaar = crypto.encrypt(raw_aadhaar) if raw_aadhaar else None
        hashed_aadhaar = AadharHashing.hash_aadhar(raw_aadhaar) if raw_aadhaar else None

        passenger = BookingPassenger(
            booking_id=booking.id,
            full_name=p['full_name'],
            age=p.get('age') or 0,  # 0 = sentinel for student passengers (no age)
            gender=gender_enum,
            phone_number=p.get('phone'),
            relationship_to_lead=p.get('relationship'),
            is_primary=p.get('is_primary', False),
            aadhar_encrypted=encrypted_aadhaar,
            aadhar_hash=hashed_aadhaar,
            student_class=p.get('student_class') or None,
        )
        db.add(passenger)

    # 5b. Create Payment ledger row for audit trail
    from app.models.payment import Payment
    from app.models.enums import PaymentStatus
    payment_record = Payment(
        booking_id=booking.id,
        payment_reference_id=draft.pg_transaction_id,  # idempotency key
        pg_order_id=draft.pg_transaction_id,
        pg_payment_id=payment_id,
        amount=draft.amount_payable,
        status=PaymentStatus.CAPTURED,
        payment_method=payment_source,
        collected_by_type=payment_source,
    )
    db.add(payment_record)

    # Update Checkout Funnel Activity log to PAYMENT_COMPLETED
    try:
        from app.models.activity_log import CheckoutFunnelLog
        f_res = await db.execute(select(CheckoutFunnelLog).where(
            (CheckoutFunnelLog.booking_public_id == draft.draft_id) |
            (CheckoutFunnelLog.booking_public_id == draft.pg_transaction_id)
        ))
        f_log = f_res.scalars().first()
        if f_log:
            f_log.funnel_stage = "PAYMENT_COMPLETED"
    except Exception as log_err:
        logger.warning(f"Could not update funnel log to PAYMENT_COMPLETED: {log_err}")

    # 6. Persist Stay Dates (if room)
    if draft.target_type == 'room':
        for sd in stay_dates:
            db.add(BookingStayDate(booking_id=booking.id, date=sd))

    # 7. Trigger document generation asynchronously
    async def _enqueue_documents_task_safe(b_id: int, p_id: str, is_fully_paid: bool):
        try:
            from app.worker import get_arq_pool
            arq_pool = await get_arq_pool()
            if arq_pool:
                await arq_pool.enqueue_job("process_post_booking_documents_task", b_id, is_fully_paid)
                logger.info(f"Enqueued post-booking documents task to ARQ for booking {p_id}")
        except Exception as arq_err:
            logger.warning(f"ARQ enqueue skipped for booking {p_id}: {arq_err}")

        # Direct in-process fallback to guarantee email & PDF document delivery
        try:
            from app.services.pdf_generator import process_post_booking_documents_task
            await process_post_booking_documents_task(None, b_id, is_fully_paid=is_fully_paid)
            logger.info(f"Successfully sent confirmation emails and generated PDFs for booking {p_id}")
        except Exception as doc_err:
            logger.error(f"Error in direct document/email generation for booking {p_id}: {doc_err}")

    if background_tasks:
        background_tasks.add_task(_enqueue_documents_task_safe, booking.id, booking.public_id, booking.status == BookingStatus.FULLY_PAID)

    # 8. Enqueue confirmation SMS via arq (Zero-DB background task)
    # get_booking_sms_payload runs NOW while db is still open — no new connection.
    # dispatch_sms_payload is enqueued to arq/Redis — retried if MSG91 is down.
    try:
        await db.flush()
        from app.services.sms_service import get_booking_sms_payload
        from app.worker import get_arq_pool
        sms_payload = await get_booking_sms_payload(booking.id, db)
        if sms_payload:
            arq_pool = await get_arq_pool()
            await arq_pool.enqueue_job("dispatch_sms_payload", sms_payload)
    except Exception as _sms_err:
        logger.warning(f"Could not enqueue confirmation SMS for booking {booking.public_id}: {_sms_err}")

    # 9. Delete Draft
    await db.delete(draft)
    await db.flush()
    return booking.public_id



# ─── PhonePe: Verify Status (polling after redirect) ─────────────────────────

@router.get("/verify-status")
async def verify_status(
    transaction_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    Frontend polls this after PhonePe redirect to check payment status.
    If success, booking is finalized.
    """
    check_res = await phonepe_service.get_transaction_status(transaction_id)
    status_str = check_res.get("status")
    payment_id = check_res.get("gateway_payment_id")

    if status_str == "PENDING":
        return {"status": "pending", "message": "Transaction is pending verification."}
    elif status_str == "FAILED":
        draft_query = select(BookingDraft).where(BookingDraft.pg_transaction_id == transaction_id).with_for_update()
        res = await db.execute(draft_query)
        draft = res.scalar_one_or_none()
        if draft:
            sse_payloads = await release_draft_inventory(draft, db)
            await db.delete(draft)
            await db.commit()

            from app.utils.sse import sse_manager
            for p in sse_payloads or []:
                await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)
        return {"status": "failed", "message": "Transaction failed."}

    # Lock draft to prevent webhook race condition
    draft_query = select(BookingDraft).where(BookingDraft.pg_transaction_id == transaction_id).with_for_update()
    res = await db.execute(draft_query)
    draft = res.scalar_one_or_none()

    if not draft:
        # Check if booking already finalized
        existing = await db.execute(
            select(Booking).where(Booking.pricing_snapshot['pg_transaction_id'].astext == transaction_id)
        )
        booking = existing.scalar_one_or_none()
        if booking:
            return {"status": "success", "booking_id": booking.public_id}

        # Check if this is a balance payment
        from app.models.payment import Payment
        from app.models.enums import PaymentStatus

        payment_stmt = select(Payment).where(
            Payment.pg_order_id == transaction_id
        ).with_for_update()
        p_res = await db.execute(payment_stmt)
        payment = p_res.scalar_one_or_none()

        if payment:
            booking_stmt = select(Booking).where(Booking.id == payment.booking_id).with_for_update()
            bk_res = await db.execute(booking_stmt)
            booking = bk_res.scalar_one()

            if payment.status != PaymentStatus.CAPTURED:
                already_captured = await db.execute(
                    select(Payment).where(
                        Payment.pg_payment_id == payment_id,
                        Payment.status == PaymentStatus.CAPTURED
                    )
                )
                if already_captured.scalar_one_or_none():
                    return {"status": "success", "booking_id": booking.public_id}

                payment.status = PaymentStatus.CAPTURED
                payment.pg_payment_id = payment_id

                from app.utils.ledger import recompute_booking_ledger
                booking = await recompute_booking_ledger(booking.id, db)

                # Enqueue SMS via arq (Zero-DB, retried if MSG91 is down)
                try:
                    from app.services.sms_service import get_booking_sms_payload
                    from app.worker import get_arq_pool
                    sms_payload = await get_booking_sms_payload(booking.id, db)
                    if sms_payload:
                        arq_pool = await get_arq_pool()
                        await arq_pool.enqueue_job("dispatch_sms_payload", sms_payload)
                except Exception as _sms_err:
                    logger.warning(f"Could not enqueue confirmation SMS for booking {booking.public_id}: {_sms_err}")

            async def _enqueue_bal_task(b_id: int, is_fully_paid: bool):
                try:
                    from app.worker import get_arq_pool
                    arq_pool = await get_arq_pool()
                    if arq_pool:
                        await arq_pool.enqueue_job("process_post_booking_documents_task", b_id, is_fully_paid)
                except Exception as arq_err:
                    logger.warning(f"ARQ enqueue skipped for balance payment: {arq_err}")

                try:
                    from app.services.pdf_generator import process_post_booking_documents_task
                    await process_post_booking_documents_task(None, b_id, is_fully_paid=is_fully_paid)
                except Exception as doc_err:
                    logger.error(f"Error in direct document/email generation for balance payment: {doc_err}")

            if payment.status == PaymentStatus.CAPTURED:
                background_tasks.add_task(_enqueue_bal_task, booking.id, booking.status == BookingStatus.FULLY_PAID)

            await db.commit()
            return {"status": "success", "booking_id": booking.public_id}

        raise HTTPException(status_code=404, detail="Draft or payment not found or expired")

    sse_payloads = []
    public_id = await _finalize_draft(draft, payment_id, db, background_tasks, sse_payloads, payment_source="PHONEPE")
    await db.commit()

    from app.utils.cache import clear_cache_prefix
    if draft.target_type == 'package':
        clear_cache_prefix("packages:list:")
        clear_cache_prefix("packages:detail:")
        
        # Invalidate packages availability Redis cache
        from app.services.redis_client import invalidate_cached_availability
        import asyncio
        if draft.variant_id:
            from app.models.package import Package, PackageVariant
            pkg_res = await db.execute(
                select(Package.slug).join(PackageVariant, PackageVariant.package_id == Package.id).where(
                    PackageVariant.id == draft.variant_id
                )
            )
            pkg_slug = pkg_res.scalar_one_or_none()
            if pkg_slug:
                asyncio.create_task(invalidate_cached_availability(pkg_slug))
                
    elif draft.target_type == 'room':
        clear_cache_prefix("rooms:list:")
        clear_cache_prefix("rooms:detail:")

    from app.utils.sse import sse_manager
    for p in sse_payloads:
        if "package_id" in p:
            await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)
        elif "room_id" in p:
            await sse_manager.broadcast_event("room", str(p["room_id"]), "INVENTORY_UPDATE", p)

    return {"status": "success", "booking_id": public_id}





# ─── PhonePe Webhook ──────────────────────────────────────────────────────────

@router.post("/webhook/phonepe")
async def phonepe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    PhonePe S2S Webhook callback.
    Finalizes the booking draft or balance payment asynchronously.
    """
    import base64
    body_bytes = await request.body()
    signature = request.headers.get("x-verify")

    try:
        data = json.loads(body_bytes)
        response_base64 = data.get("response")
    except Exception:
        logger.error("PhonePe Webhook failed to parse raw request JSON.")
        raise HTTPException(status_code=400, detail="Invalid request JSON")

    if not response_base64:
        raise HTTPException(status_code=400, detail="Missing response payload")

    is_valid = phonepe_service.verify_webhook_signature(response_base64, signature)
    if not is_valid:
        logger.warning("PhonePe Webhook signature verification failed.")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        decoded_bytes = base64.b64decode(response_base64)
        payload = json.loads(decoded_bytes)
    except Exception as e:
        logger.error(f"PhonePe Webhook failed to decode/parse base64: {e}")
        raise HTTPException(status_code=400, detail="Invalid base64 payload")

    success = payload.get("success")
    code = payload.get("code")
    payload_data = payload.get("data", {})

    merchant_txn_id = payload_data.get("merchantTransactionId")
    gateway_payment_id = payload_data.get("transactionId")
    payment_instrument = payload_data.get("paymentInstrument", {}).get("type")

    if not merchant_txn_id:
        logger.warning("PhonePe Webhook payload missing merchantTransactionId")
        return {"status": "ok"}

    try:
        if success and code == "PAYMENT_SUCCESS":
            draft_query = select(BookingDraft).where(BookingDraft.pg_transaction_id == merchant_txn_id).with_for_update()
            res = await db.execute(draft_query)
            draft = res.scalar_one_or_none()

            if draft:
                sse_payloads = []
                target_type = draft.target_type
                public_id = await _finalize_draft(draft, gateway_payment_id, db, sse_payloads=sse_payloads, payment_source="PHONEPE")
                await db.commit()

                from app.utils.cache import clear_cache_prefix
                if target_type == 'package':
                    clear_cache_prefix("packages:list:")
                    clear_cache_prefix("packages:detail:")
                    
                    # Invalidate packages availability Redis cache
                    from app.services.redis_client import invalidate_cached_availability
                    import asyncio
                    if draft.variant_id:
                        from app.models.package import Package, PackageVariant
                        pkg_res = await db.execute(
                            select(Package.slug).join(PackageVariant, PackageVariant.package_id == Package.id).where(
                                PackageVariant.id == draft.variant_id
                            )
                        )
                        pkg_slug = pkg_res.scalar_one_or_none()
                        if pkg_slug:
                            asyncio.create_task(invalidate_cached_availability(pkg_slug))
                            
                elif target_type == 'room':
                    clear_cache_prefix("rooms:list:")
                    clear_cache_prefix("rooms:detail:")

                from app.utils.sse import sse_manager
                for p in sse_payloads:
                    if "package_id" in p:
                        await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)
                    elif "room_id" in p:
                        await sse_manager.broadcast_event("room", str(p["room_id"]), "INVENTORY_UPDATE", p)

                finalized_booking = await db.execute(
                    select(Booking).where(Booking.public_id == public_id).limit(1)
                )
                booking = finalized_booking.scalar_one_or_none()
                if booking:
                    try:
                        from app.worker import get_arq_pool
                        arq_pool = await get_arq_pool()
                        await arq_pool.enqueue_job("process_post_booking_documents_task", booking.id, booking.status == BookingStatus.FULLY_PAID)
                    except Exception as arq_err:
                        logger.warning(f"Failed to enqueue document tasks from PhonePe webhook: {arq_err}")
                logger.info(f"PhonePe Webhook finalized booking for transaction {merchant_txn_id}")
            else:
                # Balance payment
                from app.models.payment import Payment
                from app.models.enums import PaymentStatus

                payment_stmt = select(Payment).where(
                    Payment.pg_order_id == merchant_txn_id
                ).with_for_update()
                p_res = await db.execute(payment_stmt)
                payment = p_res.scalar_one_or_none()

                if payment and payment.status != PaymentStatus.CAPTURED:
                    payment.status = PaymentStatus.CAPTURED
                    payment.pg_payment_id = gateway_payment_id
                    if payment_instrument:
                        payment.payment_method = payment_instrument

                    from app.utils.ledger import recompute_booking_ledger
                    booking = await recompute_booking_ledger(payment.booking_id, db)

                    try:
                        from app.worker import get_arq_pool
                        arq_pool = await get_arq_pool()
                        await arq_pool.enqueue_job("process_post_booking_documents_task", booking.id, booking.status == BookingStatus.FULLY_PAID)
                    except Exception as arq_err:
                        logger.warning(f"Failed to enqueue documents task from PhonePe webhook: {arq_err}")

                    await db.commit()
                    logger.info(f"PhonePe Webhook finalized balance payment for transaction {merchant_txn_id}")
        else:
            # Payment failed
            draft_query = select(BookingDraft).where(BookingDraft.pg_transaction_id == merchant_txn_id).with_for_update()
            res = await db.execute(draft_query)
            draft = res.scalar_one_or_none()
            if draft:
                sse_payloads = await release_draft_inventory(draft, db)
                await db.delete(draft)
                await db.commit()

                from app.utils.sse import sse_manager
                for p in sse_payloads or []:
                    await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)
                logger.info(f"PhonePe Webhook released draft {draft.draft_id} after payment failure")
            else:
                from app.models.payment import Payment
                from app.models.enums import PaymentStatus

                payment_stmt = select(Payment).where(
                    Payment.pg_order_id == merchant_txn_id
                ).with_for_update()
                p_res = await db.execute(payment_stmt)
                payment = p_res.scalar_one_or_none()
                if payment and payment.status != PaymentStatus.CAPTURED:
                    payment.status = PaymentStatus.FAILED
                    payment.error_code = code
                    payment.error_description = payload.get("message")
                    await db.commit()
                    logger.info(f"PhonePe Webhook marked balance payment failed for transaction {merchant_txn_id}")
    except Exception as e:
        logger.error(f"PhonePe Webhook processing failed: {str(e)}")
        await db.rollback()
        return {"status": "error"}

    return {"status": "ok"}



