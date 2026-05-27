from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal
import uuid
import json
from loguru import logger
from sqlalchemy import select, text
from pydantic import BaseModel

from app.db.session import get_db
from app.models.booking import Booking, BookingDraft, BookingPassenger, BookingStayDate
from app.models.package import PackageVariantInventory
from app.models.room import RoomSlotInventory
from app.models.coupon import Coupon
from app.models.enums import BookingStatus, BookingSource, GenderType
from app.services.razorpay_client import razorpay_service
from app.core.security import AadharCryptography, AadharHashing
from app.core.timezone import get_ist_now

router = APIRouter()

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

async def _finalize_draft(
    draft: BookingDraft,
    payment_id: str,
    db: AsyncSession,
    background_tasks: BackgroundTasks | None = None,
    sse_payloads: list = None
) -> str:
    """
    Idempotent function to convert BookingDraft to Booking.
    Returns the public_id of the generated Booking.
    """
    # 1. Double check if a booking already exists for this order_id
    existing_booking = await db.execute(
        select(Booking).where(Booking.pricing_snapshot['razorpay_order_id'].astext == draft.razorpay_order_id)
    )
    existing = existing_booking.scalar_one_or_none()
    if existing:
        logger.info(f"Booking already finalized for order {draft.razorpay_order_id}")
        return existing.public_id

    # 2. Promote reserved_count -> booked_count in inventory
    if draft.target_type == 'package':
        inv_query = select(PackageVariantInventory).where(
            PackageVariantInventory.variant_id == draft.variant_id,
            PackageVariantInventory.date == draft.travel_date
        ).with_for_update()
        inv_res = await db.execute(inv_query)
        inventory = inv_res.scalar_one_or_none()
        if inventory:
            # Shift quantity from reserved to booked
            inventory.reserved_count = max(0, inventory.reserved_count - draft.quantity)
            inventory.booked_count += draft.quantity
            
            await db.flush()
            if sse_payloads is not None:
                import time
                from app.core.timezone import get_ist_now
                # effective price lookup requires PackageVariant. We can retrieve it, or just omit if frontend handles patch
                # But strict payload rule says full payload. Let's fetch it.
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
        # Re-evaluate stay dates from payload
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
        # Calculate required rooms
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
    snapshot["razorpay_order_id"] = draft.razorpay_order_id
    snapshot["razorpay_payment_id"] = payment_id
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

    # 5. Calculate payment status based on TOURIST perspective
    # The database must explicitly reflect what the tourist sees (the full amount).
    # The agent's actual payment amount is properly tracked via the Payment table records.
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
        # Determine if Boat Ride (TRIP -> SS, TOUR -> BT based on previous discussion, wait:
        # user said: "as we did a tour and trip", I proposed TOUR = BT, TRIP = SS
        # Let's fetch PackageType
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
            # Default to Boat Ride (TOUR)
            seq_res = await db.execute(text("SELECT nextval('booking_seq_bt')"))
            seq_val = seq_res.scalar()
            public_id = f"TBT_BT_{seq_val}"

    # 6. Generate actual Booking
    booking = Booking(
        public_id=public_id,
        user_id=draft.user_id,
        agent_id=draft.agent_id,
        source=BookingSource.AGENT if draft.agent_id else BookingSource.PUBLIC,
        variant_id=draft.variant_id,
        room_variant_id=draft.room_variant_id,
        travel_date=draft.travel_date,
        adult_count=draft.checkout_payload.get('adult_count') or draft.quantity,
        child_count=draft.checkout_payload.get('child_count') or 0,
        has_refreshment_addon=draft.checkout_payload.get('has_refreshment_addon', False),
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

        # Aadhaar: encrypt only if provided (optional for children <18)
        raw_aadhaar = (p.get('aadhaar') or '').strip()
        encrypted_aadhaar = crypto.encrypt(raw_aadhaar) if raw_aadhaar else None
        hashed_aadhaar = AadharHashing.hash_aadhar(raw_aadhaar) if raw_aadhaar else None

        passenger = BookingPassenger(
            booking_id=booking.id,
            full_name=p['full_name'],
            age=p['age'],
            gender=gender_enum,
            phone_number=p.get('phone'),
            relationship_to_lead=p.get('relationship'),
            is_primary=p.get('is_primary', False),
            aadhar_encrypted=encrypted_aadhaar,
            aadhar_hash=hashed_aadhaar,
        )
        db.add(passenger)

    # 5b. Create Payment record for audit trail
    from app.models.payment import Payment
    from app.models.enums import PaymentStatus
    payment_record = Payment(
        booking_id=booking.id,
        razorpay_order_id=draft.razorpay_order_id,
        razorpay_payment_id=payment_id,
        amount=draft.amount_payable,
        status=PaymentStatus.CAPTURED,
        payment_method="RAZORPAY",
    )
    db.add(payment_record)

    # 6. Persist Stay Dates (if room)
    if draft.target_type == 'room':
        for sd in stay_dates:
            db.add(BookingStayDate(booking_id=booking.id, date=sd))

    # 7. Trigger ticket and invoice PDF generation tasks asynchronously (fire and forget)
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


@router.post("/verify-payment")
async def verify_payment(
    request: VerifyPaymentRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    Frontend calls this immediately after Razorpay success callback.
    Verifies signature and finalizes the booking draft or balance payment.
    """
    is_valid = razorpay_service.verify_signature(
        order_id=request.razorpay_order_id,
        payment_id=request.razorpay_payment_id,
        signature=request.razorpay_signature
    )
    
    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid payment signature")

    # Lock draft to prevent webhook race condition
    draft_query = select(BookingDraft).where(BookingDraft.razorpay_order_id == request.razorpay_order_id).with_for_update()
    res = await db.execute(draft_query)
    draft = res.scalar_one_or_none()

    if not draft:
        # Check if booking already finalized by webhook
        existing = await db.execute(
            select(Booking).where(Booking.pricing_snapshot['razorpay_order_id'].astext == request.razorpay_order_id)
        )
        booking = existing.scalar_one_or_none()
        if booking:
            return {"status": "success", "booking_id": booking.public_id}
            
        # Check if this is a balance payment
        from app.models.payment import Payment
        from app.models.enums import PaymentStatus
        from sqlalchemy import func
        
        payment_stmt = select(Payment).where(
            Payment.razorpay_order_id == request.razorpay_order_id
        ).with_for_update()
        p_res = await db.execute(payment_stmt)
        payment = p_res.scalar_one_or_none()
        
        if payment:
            booking_stmt = select(Booking).where(Booking.id == payment.booking_id).with_for_update()
            bk_res = await db.execute(booking_stmt)
            booking = bk_res.scalar_one()
            
            if payment.status == PaymentStatus.CREATED:
                captured_stmt = select(func.count(Payment.id)).where(
                    Payment.booking_id == booking.id,
                    Payment.status == PaymentStatus.CAPTURED
                )
                captured_res = await db.execute(captured_stmt)
                captured_count = captured_res.scalar_one() or 0
                
                if captured_count >= 2:
                    raise HTTPException(status_code=400, detail="Maximum payment attempts reached for this booking")
                
                payment.status = PaymentStatus.CAPTURED
                payment.razorpay_payment_id = request.razorpay_payment_id
                payment.razorpay_signature = request.razorpay_signature
                
                booking.paid_amount += payment.amount
                booking.remaining_balance = max(Decimal("0.00"), booking.total_amount - booking.paid_amount)
                
                if booking.remaining_balance <= Decimal("0.01"):
                    booking.status = BookingStatus.FULLY_PAID
                
                await db.flush()
                
            # Use background tasks for balance payments too to prevent blocking
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
    public_id = await _finalize_draft(draft, request.razorpay_payment_id, db, background_tasks, sse_payloads)
    await db.commit()
    
    from app.utils.sse import sse_manager
    for p in sse_payloads:
        await sse_manager.broadcast_event("package", str(p["package_id"]), "INVENTORY_UPDATE", p)
    
    return {"status": "success", "booking_id": public_id}


@router.post("/webhook/razorpay")
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Final Authority for payment confirmation.
    Verifies HMAC signature before processing.
    """
    payload = await request.body()
    signature = request.headers.get("x-razorpay-signature")
    
    # Verify webhook HMAC signature
    if not razorpay_service.verify_webhook_signature(payload, signature or ""):
        logger.warning(f"Webhook signature verification failed. Rejecting payload.")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    
    try:
        data = json.loads(payload)
        event = data.get("event")
        
        if event == "payment.captured" or event == "order.paid":
            payment_entity = data.get("payload", {}).get("payment", {}).get("entity", {})
            order_id = payment_entity.get("order_id")
            payment_id = payment_entity.get("id")
            
            if order_id:
                # Lock draft
                draft_query = select(BookingDraft).where(BookingDraft.razorpay_order_id == order_id).with_for_update()
                res = await db.execute(draft_query)
                draft = res.scalar_one_or_none()

                if draft:
                    sse_payloads = []
                    public_id = await _finalize_draft(draft, payment_id, db, sse_payloads=sse_payloads)
                    await db.commit()
                    
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
                            logger.warning(f"Failed to enqueue post-booking documents task from webhook: {arq_err}")
                    logger.info(f"Webhook finalized booking for order {order_id}")
                else:
                    # Check if this is a balance payment
                    from app.models.payment import Payment
                    from app.models.enums import PaymentStatus
                    from sqlalchemy import func
                    
                    payment_stmt = select(Payment).where(
                        Payment.razorpay_order_id == order_id
                    ).with_for_update()
                    p_res = await db.execute(payment_stmt)
                    payment = p_res.scalar_one_or_none()
                    
                    if payment and payment.status == PaymentStatus.CREATED:
                        booking_stmt = select(Booking).where(Booking.id == payment.booking_id).with_for_update()
                        bk_res = await db.execute(booking_stmt)
                        booking = bk_res.scalar_one()
                        
                        captured_stmt = select(func.count(Payment.id)).where(
                            Payment.booking_id == booking.id,
                            Payment.status == PaymentStatus.CAPTURED
                        )
                        captured_res = await db.execute(captured_stmt)
                        captured_count = captured_res.scalar_one() or 0
                        
                        if captured_count < 2:
                            payment.status = PaymentStatus.CAPTURED
                            payment.razorpay_payment_id = payment_id
                            
                            booking.paid_amount += payment.amount
                            booking.remaining_balance = max(Decimal("0.00"), booking.total_amount - booking.paid_amount)
                            
                            if booking.remaining_balance <= Decimal("0.01"):
                                booking.status = BookingStatus.FULLY_PAID
                            
                            await db.flush()
                            
                            try:
                                from app.worker import get_arq_pool
                                arq_pool = await get_arq_pool()
                                await arq_pool.enqueue_job("process_post_booking_documents_task", booking.id, booking.status == BookingStatus.FULLY_PAID)
                            except Exception as arq_err:
                                logger.warning(f"Failed to enqueue post-booking documents task from webhook: {arq_err}")
                                
                            await db.commit()
                            logger.info(f"Webhook finalized balance payment for order {order_id}")
                    
    except Exception as e:
        logger.error(f"Webhook processing failed: {str(e)}")
        await db.rollback()
        return {"status": "error"}
        
    return {"status": "ok"}
