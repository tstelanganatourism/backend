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
from app.services.cashfree_client import cashfree_service
from app.core.security import AadharCryptography, AadharHashing
from app.core.timezone import get_ist_now

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
                import time
                from app.core.timezone import get_ist_now
                from app.models.package import PackageVariant
                v_res = await db.execute(select(PackageVariant).where(PackageVariant.id == draft.variant_id))
                variant = v_res.scalar_one_or_none()
                if variant:
                    from app.api.v1.public_packages import get_effective_package_prices
                    eff_adult, eff_child = get_effective_package_prices(variant.adult_price, variant.child_price, inventory.price_override)
                    sse_payloads.append({
                        "version": int(time.time() * 1000),
                        "timestamp": get_ist_now().isoformat(),
                        "package_id": variant.package_id,
                        "travel_date": str(draft.travel_date),
                        "available": inventory.total_capacity - (inventory.booked_count + inventory.reserved_count),
                        "reserved": inventory.reserved_count,
                        "booked": inventory.booked_count,
                        "is_closed": inventory.is_closed,
                        "effective_adult_price": float(eff_adult),
                        "effective_child_price": float(eff_child),
                        "variant_id": draft.variant_id
                    })

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

    # 3. Prepare pricing snapshot
    snapshot = draft.pricing_snapshot
    snapshot["pg_transaction_id"] = draft.pg_transaction_id
    snapshot["pg_payment_id"] = payment_id
    snapshot["payment_gateway"] = payment_source
    if draft.target_type == 'room':
        snapshot["slot_start"] = draft.checkout_payload.get('slot_start')
        snapshot["slot_end"] = draft.checkout_payload.get('slot_end')

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
        public_id = f"TBT_AC_{seq_val}"
    else:
        from app.models.package import PackageVariant, Package
        from app.models.enums import PackageType
        pkg_res = await db.execute(
            select(Package.type)
            .join(PackageVariant, PackageVariant.package_id == Package.id)
            .where(PackageVariant.id == draft.variant_id)
        )
        pkg_type = pkg_res.scalar_one_or_none()

        if pkg_type == PackageType.TRIP:
            seq_res = await db.execute(text("SELECT nextval('booking_seq_ss')"))
            seq_val = seq_res.scalar()
            public_id = f"TBT_SS_{seq_val}"
        else:
            seq_res = await db.execute(text("SELECT nextval('booking_seq_bt')"))
            seq_val = seq_res.scalar()
            public_id = f"TBT_BT_{seq_val}"

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

    # 6. Persist Stay Dates (if room)
    if draft.target_type == 'room':
        for sd in stay_dates:
            db.add(BookingStayDate(booking_id=booking.id, date=sd))

    # 7. Trigger document generation asynchronously
    async def _enqueue_documents_task_safe(b_id: int, p_id: str, is_fully_paid: bool):
        try:
            from app.worker import get_arq_pool
            arq_pool = await get_arq_pool()
            await arq_pool.enqueue_job("process_post_booking_documents_task", b_id, is_fully_paid)
            logger.info(f"Successfully enqueued post-booking documents task for booking {p_id}")
        except Exception as arq_err:
            logger.warning(f"Failed to enqueue post-booking documents background task: {arq_err}")

    if background_tasks:
        background_tasks.add_task(_enqueue_documents_task_safe, booking.id, booking.public_id, booking.status == BookingStatus.FULLY_PAID)

    # 8. Delete Draft
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
            from app.workers.draft_cleanup import release_draft_inventory
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

            async def _enqueue_bal_task(b_id: int, is_fully_paid: bool):
                try:
                    from app.worker import get_arq_pool
                    arq_pool = await get_arq_pool()
                    await arq_pool.enqueue_job("process_post_booking_documents_task", b_id, is_fully_paid)
                except Exception as arq_err:
                    logger.warning(f"Failed to enqueue post-booking documents task for balance payment: {arq_err}")

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
    elif draft.target_type == 'room':
        clear_cache_prefix("rooms:list:")
        clear_cache_prefix("rooms:detail:")

    from app.utils.sse import sse_manager
    for p in sse_payloads:
        await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)

    return {"status": "success", "booking_id": public_id}


# ─── Cashfree: Verify Status (polling after popup closes) ────────────────────

@router.get("/verify-cashfree-status")
async def verify_cashfree_status(
    order_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    Frontend polls this after the Cashfree popup closes to check payment status.
    order_id = the pg_transaction_id (Cashfree order ID) created during checkout.
    """
    check_res = await cashfree_service.get_order_status(order_id)
    order_status = check_res.get("status")  # PAID | ACTIVE | EXPIRED | CANCELLED | ERROR
    payment_id = check_res.get("pg_payment_id")

    if order_status == "ACTIVE":
        return {"status": "pending", "message": "Payment is still being processed."}
    elif order_status in ("EXPIRED", "CANCELLED", "ERROR"):
        # Release draft inventory
        draft_query = select(BookingDraft).where(BookingDraft.pg_transaction_id == order_id).with_for_update()
        res = await db.execute(draft_query)
        draft = res.scalar_one_or_none()
        if draft:
            from app.workers.draft_cleanup import release_draft_inventory
            sse_payloads = await release_draft_inventory(draft, db)
            await db.delete(draft)
            await db.commit()
            from app.utils.sse import sse_manager
            for p in sse_payloads or []:
                await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)
        return {"status": "failed", "message": "Payment was not completed."}

    if order_status != "PAID":
        return {"status": "pending", "message": "Payment status unknown. Please wait."}

    # PAID — finalize booking
    draft_query = select(BookingDraft).where(BookingDraft.pg_transaction_id == order_id).with_for_update()
    res = await db.execute(draft_query)
    draft = res.scalar_one_or_none()

    if not draft:
        # Already finalized?
        existing = await db.execute(
            select(Booking).where(Booking.pricing_snapshot['pg_transaction_id'].astext == order_id)
        )
        booking = existing.scalar_one_or_none()
        if booking:
            return {"status": "success", "booking_id": booking.public_id}

        # Balance payment?
        from app.models.payment import Payment
        from app.models.enums import PaymentStatus
        payment_stmt = select(Payment).where(Payment.pg_order_id == order_id).with_for_update()
        p_res = await db.execute(payment_stmt)
        payment = p_res.scalar_one_or_none()

        if payment:
            booking_stmt = select(Booking).where(Booking.id == payment.booking_id).with_for_update()
            bk_res = await db.execute(booking_stmt)
            booking = bk_res.scalar_one()

            if payment.status != PaymentStatus.CAPTURED:
                payment.status = PaymentStatus.CAPTURED
                payment.pg_payment_id = payment_id
                from app.utils.ledger import recompute_booking_ledger
                booking = await recompute_booking_ledger(booking.id, db)

            async def _enqueue_cashfree_bal(b_id: int, is_fully_paid: bool):
                try:
                    from app.worker import get_arq_pool
                    arq_pool = await get_arq_pool()
                    await arq_pool.enqueue_job("process_post_booking_documents_task", b_id, is_fully_paid)
                except Exception as arq_err:
                    logger.warning(f"Failed to enqueue documents task: {arq_err}")

            background_tasks.add_task(_enqueue_cashfree_bal, booking.id, booking.status == BookingStatus.FULLY_PAID)
            await db.commit()
            return {"status": "success", "booking_id": booking.public_id}

        raise HTTPException(status_code=404, detail="Draft or payment not found or expired")

    sse_payloads = []
    public_id = await _finalize_draft(draft, payment_id, db, background_tasks, sse_payloads, payment_source="CASHFREE")
    await db.commit()

    from app.utils.cache import clear_cache_prefix
    if draft.target_type == 'package':
        clear_cache_prefix("packages:list:")
        clear_cache_prefix("packages:detail:")
    elif draft.target_type == 'room':
        clear_cache_prefix("rooms:list:")
        clear_cache_prefix("rooms:detail:")

    from app.utils.sse import sse_manager
    for p in sse_payloads:
        await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)

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
                elif target_type == 'room':
                    clear_cache_prefix("rooms:list:")
                    clear_cache_prefix("rooms:detail:")

                from app.utils.sse import sse_manager
                for p in sse_payloads:
                    await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)

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
                from app.workers.draft_cleanup import release_draft_inventory
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


# ─── Cashfree Webhook (optional — we use polling as primary) ──────────────────

@router.post("/webhook/cashfree")
async def cashfree_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Cashfree S2S Webhook — fires on payment events.
    We use polling (verify-cashfree-status) as primary, but this handles edge cases.
    """
    body_bytes = await request.body()
    raw_body = body_bytes.decode("utf-8")
    timestamp = request.headers.get("x-webhook-timestamp", "")
    signature = request.headers.get("x-webhook-signature", "")

    is_valid = cashfree_service.verify_webhook_signature(timestamp, raw_body, signature)
    if not is_valid:
        logger.warning("Cashfree Webhook signature verification failed.")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        data = json.loads(raw_body)
        event_type = data.get("type", "")
        order_data = data.get("data", {}).get("order", {})
        payment_data = data.get("data", {}).get("payment", {})

        order_id = order_data.get("order_id")
        payment_status = payment_data.get("payment_status")  # SUCCESS | FAILED | USER_DROPPED
        cf_payment_id = str(payment_data.get("cf_payment_id", "") or "")

        if not order_id:
            return {"status": "ok"}

        if payment_status == "SUCCESS":
            draft_query = select(BookingDraft).where(BookingDraft.pg_transaction_id == order_id).with_for_update()
            res = await db.execute(draft_query)
            draft = res.scalar_one_or_none()

            if draft:
                sse_payloads = []
                target_type = draft.target_type
                public_id = await _finalize_draft(draft, cf_payment_id, db, sse_payloads=sse_payloads, payment_source="CASHFREE")
                await db.commit()

                from app.utils.cache import clear_cache_prefix
                if target_type == 'package':
                    clear_cache_prefix("packages:list:")
                    clear_cache_prefix("packages:detail:")
                elif target_type == 'room':
                    clear_cache_prefix("rooms:list:")
                    clear_cache_prefix("rooms:detail:")

                from app.utils.sse import sse_manager
                for p in sse_payloads:
                    await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)

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
                        logger.warning(f"Failed to enqueue document tasks from Cashfree webhook: {arq_err}")
                logger.info(f"Cashfree Webhook finalized booking for order {order_id}")
            else:
                # Balance payment
                from app.models.payment import Payment
                from app.models.enums import PaymentStatus
                payment_stmt = select(Payment).where(Payment.pg_order_id == order_id).with_for_update()
                p_res = await db.execute(payment_stmt)
                payment = p_res.scalar_one_or_none()

                if payment and payment.status != PaymentStatus.CAPTURED:
                    payment.status = PaymentStatus.CAPTURED
                    payment.pg_payment_id = cf_payment_id
                    from app.utils.ledger import recompute_booking_ledger
                    booking = await recompute_booking_ledger(payment.booking_id, db)
                    try:
                        from app.worker import get_arq_pool
                        arq_pool = await get_arq_pool()
                        await arq_pool.enqueue_job("process_post_booking_documents_task", booking.id, booking.status == BookingStatus.FULLY_PAID)
                    except Exception as arq_err:
                        logger.warning(f"Failed to enqueue documents task from Cashfree webhook: {arq_err}")
                    await db.commit()
                    logger.info(f"Cashfree Webhook finalized balance payment for order {order_id}")

        elif payment_status in ("FAILED", "USER_DROPPED"):
            # Release draft inventory if payment failed
            draft_query = select(BookingDraft).where(BookingDraft.pg_transaction_id == order_id).with_for_update()
            res = await db.execute(draft_query)
            draft = res.scalar_one_or_none()
            if draft:
                from app.workers.draft_cleanup import release_draft_inventory
                sse_payloads = await release_draft_inventory(draft, db)
                await db.delete(draft)
                await db.commit()
                from app.utils.sse import sse_manager
                for p in sse_payloads or []:
                    await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)
                logger.info(f"Cashfree Webhook released draft after payment {payment_status} for order {order_id}")

    except Exception as e:
        logger.error(f"Cashfree Webhook processing failed: {str(e)}")
        await db.rollback()
        return {"status": "error"}

    return {"status": "ok"}
