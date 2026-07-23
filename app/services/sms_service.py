import logging
import os
from typing import Optional, Dict, Any
import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)

# ─── Short Package Names (DLT ≤30 chars) ─────────────────────────────────────

PACKAGE_SHORT_NAMES = {
    "Rajahmundry to Bhadrachalam 1-Day Drop Package": "RJY-BC 1D Drop Pkg",
    "Bhadrachalam to Rajahmundry 1-Day Drop Package": "BC-RJY 1D Drop Pkg",
    "Rajahmundry to Papikondalu 1-Day Tour Package": "RJY-PKD 1D Tour",
    "Bhadrachalam to Papikondalu 1-Day Tour Package": "BC-PKD 1D Tour",
    "Pochavaram to Papikondalu 1-Day Tour Package": "Pochavaram-PKD 1D Tour",
    "Papikondalu 1-Day Tour From Rajahmundry (NO TRANSPORT)": "RJY-PKD NoTrans",
    "Rajahmundry to Papikondalu 2-Days Tour Package": "RJY-PKD 2D Tour",
    "Bhadrachalam to Papikondalu 2-Days Tour Package": "BC-PKD 2D Tour",
    "Pochavaram to Papikondalu 2-Days Tour Package": "Pochavaram-PKD 2D Tour",
    "PAPIKONDALU 2-DAYS TOUR PACKAGE FROM RAJAHMUNDRY (WITHOUT TRANSPORT)": "RJY-PKD 2D NoTrans",
    "PAPIKONDALU 2-DAYS TOUR PACKAGE FROM BHADRACHALAM (WITHOUT TRANSPORT)": "BC-PKD 2D NoTrans",
    "MAREDUMILLI - BHADRACHALAM-PAPIKONDALU (WITH-OUT TRANSPORT)": "Maredumilli-BC-PKD NoTrans",
    "Pochavaram to Papikondalu 1-Day Tour Package(School package)(LKG To 10th)": "Pochavaram-PKD School Pkg",
}

# ─── DLT-Approved Template IDs ───────────────────────────────────────────────

TEMPLATES = {
    "TSBOAT_OTP":                  settings.MSG91_OTP_TEMPLATE_ID,
    "TSBOAT_ROOM_CONFIRM":         settings.MSG91_ROOM_CONFIRM_TEMPLATE_ID,
    "TSBOAT_ROOM_REMINDER":        settings.MSG91_ROOM_REMINDER_TEMPLATE_ID,
    "TSBOAT_CONFIRMATION_FULL":    settings.MSG91_CONFIRMATION_FULL_TEMPLATE_ID,
    "TSBOAT_CONFIRMATION_PARTIAL": settings.MSG91_CONFIRMATION_PARTIAL_TEMPLATE_ID,
    "TSBOAT_TRAVEL_REMINDER":      settings.MSG91_TRAVEL_REMINDER_TEMPLATE_ID,
}

SITE_BASE = "https://tstelanganatourism.com"


def get_short_package_name(package_title: str) -> str:
    """Returns a DLT-compliant short name (≤30 chars) for a package."""
    short = PACKAGE_SHORT_NAMES.get(package_title, package_title)
    return short[:27] + "..." if len(short) > 30 else short


def _ticket_url(public_id: str) -> str:
    return f"{SITE_BASE}/print/ticket/{public_id}"


def _location_url(public_id: str) -> str:
    return f"{SITE_BASE}/location?ticket={public_id}"


# ─── Core MSG91 sender ───────────────────────────────────────────────────────

async def send_msg91_sms(mobile: str, template_id: str, variables: Dict[str, Any]) -> bool:
    """Generic function to send SMS via MSG91 Flow API."""
    auth_key = settings.MSG91_AUTH_KEY
    if not auth_key:
        logger.error("MSG91_AUTH_KEY not configured. SMS not sent.")
        return False

    mobile_str = str(mobile).strip().replace(" ", "").replace("-", "")
    if len(mobile_str) == 10 and mobile_str.isdigit():
        mobile_str = "91" + mobile_str
    elif not mobile_str.startswith("91"):
        mobile_str = "91" + mobile_str[-10:]

    payload = {
        "template_id": template_id,
        "short_url": "0",
        "recipients": [{"mobiles": mobile_str, **variables}],
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authkey": auth_key,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://control.msg91.com/api/v5/flow/",
                json=payload,
                headers=headers,
                timeout=10.0,
            )
            resp.raise_for_status()
            logger.info(f"MSG91 SMS sent to {mobile_str}. Response: {resp.text}")
            return True
    except Exception as e:
        logger.error(f"MSG91 SMS failed to {mobile_str}: {e}")
        return False


# ─── OTP SMS ─────────────────────────────────────────────────────────────────

async def send_otp_sms(phone: str, otp: str) -> bool:
    """Send a login OTP via SMS using the TSBOAT_OTP template."""
    return await send_msg91_sms(
        mobile=phone,
        template_id=TEMPLATES["TSBOAT_OTP"],
        variables={"var1": otp, "var2": "5"},  # var2 = expiry minutes
    )


# ─── Booking Confirmation SMS ─────────────────────────────────────────────────

async def send_booking_confirmation_sms(
    customer_name: str,
    customer_phone: str,
    public_id: str,
    package_title: str,
    travel_date_str: str,
    passenger_count: int,
    paid_amount: str,
    total_amount: str,
    is_partial: bool,
    passenger_count_str: str = None,
) -> bool:
    """Send booking confirmation SMS. Uses full or partial template."""
    short_name = get_short_package_name(package_title)

    if is_partial:
        remaining = f"{float(total_amount) - float(paid_amount):.2f}"
        paid_fmt = f"{float(paid_amount):.2f}"
        return await send_msg91_sms(
            mobile=customer_phone,
            template_id=TEMPLATES["TSBOAT_CONFIRMATION_PARTIAL"],
            variables={
                "var1": customer_name,
                "var2": public_id,
                "var3": paid_fmt,
                "var4": remaining,
                "var5": travel_date_str,
                "var6": public_id,
                "var7": public_id,
            },
        )
    else:
        paid_fmt = f"{float(paid_amount):.2f}"
        total_fmt = f"{float(total_amount):.2f}"
        return await send_msg91_sms(
            mobile=customer_phone,
            template_id=TEMPLATES["TSBOAT_CONFIRMATION_FULL"],
            variables={
                "var1": customer_name,
                "var2": public_id,
                "var3": short_name,
                "var4": travel_date_str,
                "var5": passenger_count_str or str(passenger_count),
                "var6": paid_fmt,
                "var7": total_fmt,
                "var8": public_id,
                "var9": public_id,
            },
        )


async def send_room_confirmation_sms(
    customer_name: str,
    customer_phone: str,
    public_id: str,
    lodge_name: str,
    room_name: str,
    checkin_date_str: str,
    checkin_time_str: str,
    checkout_date_str: str,
    checkout_time_str: str,
    paid_amount_str: str,
    total_amount_str: str,
) -> bool:
    """Send room booking confirmation SMS using the approved DLT template."""
    return await send_msg91_sms(
        mobile=customer_phone,
        template_id=TEMPLATES["TSBOAT_ROOM_CONFIRM"],
        variables={
            "var1": customer_name,
            "var2": public_id,
            "var3": lodge_name[:30],
            "var4": room_name[:30],
            "var5": checkin_date_str,
            "var6": checkin_time_str,
            "var7": checkout_date_str,
            "var8": checkout_time_str,
            "var9": paid_amount_str,
            "var10": total_amount_str,
            "var11": public_id,
        },
    )


# ─── Travel Day Reminder SMS ─────────────────────────────────────────────────

async def send_travel_reminder_sms(
    customer_name: str,
    customer_phone: str,
    public_id: str,
    package_title: str,
    boarding_title: str,
    boarding_time: str,
    boarding_landmark: str,
    boarding_phone: str,
) -> bool:
    """Send a travel-day reminder SMS for a package booking using the approved DLT template."""
    return await send_msg91_sms(
        mobile=customer_phone,
        template_id=TEMPLATES["TSBOAT_TRAVEL_REMINDER"],
        variables={
            "var1": customer_name,
            "var2": public_id,
            "var3": get_short_package_name(package_title),
            "var4": boarding_title[:30] if boarding_title else "Boarding Point",
            "var5": boarding_time[:15] if boarding_time else "7:30 AM",
            "var6": boarding_landmark[:50] if boarding_landmark else "Near SBI ATM",
            "var7": boarding_phone[:15] if boarding_phone else "9951369573",
            "var8": public_id,
            "var9": public_id,
        },
    )


async def send_room_reminder_sms(
    customer_name: str,
    customer_phone: str,
    public_id: str,
    lodge_name: str,
    checkin_detail: str,
) -> bool:
    """Send a travel-day reminder SMS for a room booking using the approved DLT template."""
    return await send_msg91_sms(
        mobile=customer_phone,
        template_id=TEMPLATES["TSBOAT_ROOM_REMINDER"],
        variables={
            "var1": customer_name,
            "var2": public_id,
            "var3": lodge_name[:30],
            "var4": checkin_detail,
            "var5": "9951369573",  # Office contact number from DLT sample
            "var6": public_id,
        },
    )


# ─── Zero-DB SMS Helpers (arq-safe) ──────────────────────────────────────────
#
# ARCHITECTURE:
#   1. get_booking_sms_payload(booking_id, db)
#      → Called INSIDE the request handler while the DB session is alive.
#      → Queries all needed fields and returns a plain dict (no SQLAlchemy objects).
#      → Does NOT open any new DB connection.
#
#   2. dispatch_sms_payload(payload)
#      → Called as an arq background task. Contains ZERO database code.
#      → Reads the pre-assembled dict and calls MSG91 via HTTP.
#      → If MSG91 is down, arq will retry automatically up to max_tries times.
#      → The payload survives server restarts because it is stored in Redis.


async def get_booking_sms_payload(
    booking_id: int,
    db,  # AsyncSession — the live request session, no new connection opened
) -> Optional[dict]:
    """
    Queries all SMS-related fields for a booking using the CALLER's DB session.
    Returns a plain serializable dict ready for dispatch_sms_payload, or None
    if any required data is missing (phone number, booking not found, etc.).

    This function must be called BEFORE db.commit() / db.close(), i.e. inside
    the request handler while the session is still open.
    """
    import re
    from datetime import timedelta
    from sqlalchemy import select as _select
    from sqlalchemy.orm import selectinload, joinedload
    from app.models.booking import Booking, BookingPassenger, BookingStayDate
    from app.models.user import User
    from app.models.enums import BookingStatus

    try:
        # ── 1. Load booking + passengers ─────────────────────────────────────
        stmt = (
            _select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.passengers))
        )
        res = await db.execute(stmt)
        booking = res.scalar_one_or_none()
        if not booking:
            logger.error(f"[SMS payload] Booking ID {booking_id} not found.")
            return None

        # ── 2. Resolve phone number ───────────────────────────────────────────
        sms_phone = None
        sms_cust_name = "Customer"

        lead_p = next((p for p in booking.passengers if p.is_primary), None)
        if not lead_p and booking.passengers:
            lead_p = booking.passengers[0]
        if lead_p:
            sms_phone = lead_p.phone_number
            sms_cust_name = lead_p.full_name

        if not sms_phone and booking.user_id:
            user_res = await db.execute(_select(User).where(User.id == booking.user_id))
            assigned_user = user_res.scalar_one_or_none()
            if assigned_user:
                sms_phone = assigned_user.phone_number
                if not sms_cust_name or sms_cust_name == "Customer":
                    sms_cust_name = assigned_user.full_name

        if not sms_phone:
            logger.warning(f"[SMS payload] No phone number for booking {booking_id}. SMS skipped.")
            return None

        clean_phone = re.sub(r"\D", "", sms_phone)
        if len(clean_phone) != 10:
            logger.warning(f"[SMS payload] Invalid phone '{sms_phone}' for booking {booking_id}.")
            return None

        # ── 3. Build shared fields ────────────────────────────────────────────
        cust_first_name = (sms_cust_name or "Customer").split()[0]
        travel_str = (
            booking.travel_date.strftime("%d-%b-%Y")
            if hasattr(booking.travel_date, "strftime")
            else str(booking.travel_date)
        )
        is_partial = booking.status != BookingStatus.FULLY_PAID

        # ── COMMISSION GUARD: Always show public (tourist) prices in SMS ──────
        # booking.paid_amount and booking.total_amount are already scaled/saved
        # as tourist-facing public amounts in the database.
        from decimal import Decimal
        public_paid = Decimal(str(booking.paid_amount))
        public_total = Decimal(str(booking.total_amount))

        paid_str = f"{float(public_paid):.2f}"
        total_str = f"{float(public_total):.2f}"

        # ── 4a. Package booking ───────────────────────────────────────────────
        if booking.variant_id:
            from app.models.package import PackageVariant
            var_stmt = (
                _select(PackageVariant)
                .options(joinedload(PackageVariant.package))
                .where(PackageVariant.id == booking.variant_id)
            )
            var_res = await db.execute(var_stmt)
            variant = var_res.scalar_one_or_none()
            package_title = (
                variant.package.title if variant and variant.package else "Boat Tour"
            )

            adult_count = booking.adult_count or 0
            child_count = booking.child_count or 0
            student_count = booking.student_count or 0
            pax_total = len(booking.passengers)
            if student_count > 0:
                pax_str = f"{student_count} Students"
            else:
                pax_str = f"{adult_count} Adults" + (
                    f", {child_count} Child" if child_count > 0 else ""
                )

            logger.info(
                f"[SMS payload] Package booking {booking.public_id} payload assembled "
                f"(is_partial={is_partial}, phone={clean_phone})"
            )
            return {
                "type": "package",
                "customer_name": cust_first_name,
                "customer_phone": clean_phone,
                "public_id": booking.public_id,
                "package_title": package_title,
                "travel_date_str": travel_str,
                "passenger_count": pax_total,
                "passenger_count_str": pax_str,
                "paid_amount": paid_str,
                "total_amount": total_str,
                "is_partial": is_partial,
            }

        # ── 4b. Room booking ──────────────────────────────────────────────────
        elif booking.room_variant_id:
            from app.models.room import RoomVariant
            rv_stmt = _select(RoomVariant).where(RoomVariant.id == booking.room_variant_id)
            rv_res = await db.execute(rv_stmt)
            rv = rv_res.scalar_one_or_none()
            lodge_name = rv.lodge_name if rv else "Lodge Stay"
            room_name = rv.variant_name if rv else "Room"

            sd_stmt = _select(BookingStayDate).where(BookingStayDate.booking_id == booking.id)
            sd_res = await db.execute(sd_stmt)
            stay_dates = [sd.date for sd in sd_res.scalars().all()]
            nights = len(stay_dates) if stay_dates else 1
            checkout_date = booking.travel_date + timedelta(days=nights)
            checkout_date_str = checkout_date.strftime("%d-%b-%Y")

            logger.info(
                f"[SMS payload] Room booking {booking.public_id} payload assembled "
                f"(phone={clean_phone})"
            )
            return {
                "type": "room",
                "customer_name": cust_first_name,
                "customer_phone": clean_phone,
                "public_id": booking.public_id,
                "lodge_name": lodge_name,
                "room_name": room_name,
                "checkin_date_str": travel_str,
                "checkin_time_str": booking.room_checkin or "11:00 AM",
                "checkout_date_str": checkout_date_str,
                "checkout_time_str": booking.room_checkout or "10:00 AM",
                "paid_amount_str": paid_str,
                "total_amount_str": total_str,
            }

        else:
            logger.warning(
                f"[SMS payload] Booking {booking_id} has neither variant_id nor "
                f"room_variant_id. No SMS payload built."
            )
            return None

    except Exception as exc:
        logger.error(f"[SMS payload] Failed to build payload for booking {booking_id}: {exc}")
        return None


async def dispatch_sms_payload(ctx, payload: dict) -> bool:
    """
    arq-registered task: sends the confirmation SMS for a payload dict that was
    pre-assembled by get_booking_sms_payload() inside the request handler.

    IMPORTANT: This function contains ZERO database code. It only makes an
    outbound HTTP request to MSG91. This means:
      - It never consumes a DB connection from the pool.
      - If MSG91 is temporarily down, arq retries automatically.
      - If the server restarts, the task is picked up from Redis on the next start.
    """
    if not payload:
        logger.warning("[SMS dispatch] Received empty payload. Skipping.")
        return False

    booking_type = payload.get("type")
    public_id = payload.get("public_id", "unknown")

    try:
        if booking_type == "package":
            logger.info(f"[SMS dispatch] Sending package confirmation for {public_id}")
            return await send_booking_confirmation_sms(
                customer_name=payload["customer_name"],
                customer_phone=payload["customer_phone"],
                public_id=payload["public_id"],
                package_title=payload["package_title"],
                travel_date_str=payload["travel_date_str"],
                passenger_count=payload["passenger_count"],
                paid_amount=payload["paid_amount"],
                total_amount=payload["total_amount"],
                is_partial=payload["is_partial"],
                passenger_count_str=payload.get("passenger_count_str"),
            )

        elif booking_type == "room":
            logger.info(f"[SMS dispatch] Sending room confirmation for {public_id}")
            return await send_room_confirmation_sms(
                customer_name=payload["customer_name"],
                customer_phone=payload["customer_phone"],
                public_id=payload["public_id"],
                lodge_name=payload["lodge_name"],
                room_name=payload["room_name"],
                checkin_date_str=payload["checkin_date_str"],
                checkin_time_str=payload["checkin_time_str"],
                checkout_date_str=payload["checkout_date_str"],
                checkout_time_str=payload["checkout_time_str"],
                paid_amount_str=payload["paid_amount_str"],
                total_amount_str=payload["total_amount_str"],
            )

        else:
            logger.error(f"[SMS dispatch] Unknown payload type '{booking_type}' for {public_id}.")
            return False

    except Exception as exc:
        logger.error(f"[SMS dispatch] Failed to send SMS for {public_id}: {exc}")
        # Re-raise so arq knows the job failed and should be retried
        raise

