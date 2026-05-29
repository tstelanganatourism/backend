"""
Centralized booking ledger recomputation utility.

This is the single source of truth for computing booking.paid_amount,
booking.remaining_balance, and booking.status from the payment ledger.

NEVER update those three fields manually in any endpoint.
ALWAYS call recompute_booking_ledger() after any payment row change.
"""
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload


async def recompute_booking_ledger(booking_id: int, db: AsyncSession):
    """
    Recomputes paid_amount, remaining_balance, and status for a booking
    by summing all CAPTURED payment ledger rows.

    Must be called inside an open transaction. Caller is responsible for commit.
    Returns the updated Booking instance (already flushed, not committed).
    """
    from app.models.booking import Booking
    from app.models.payment import Payment
    from app.models.enums import BookingStatus, PaymentStatus

    # Lock booking row + eagerly load payments to avoid N+1
    stmt = (
        select(Booking)
        .options(selectinload(Booking.payments))
        .where(Booking.id == booking_id)
        .with_for_update()
    )
    result = await db.execute(stmt)
    booking = result.scalar_one()

    # Only count CAPTURED (successful) entries
    captured = [p for p in booking.payments if p.status == PaymentStatus.CAPTURED]
    total_paid = sum(Decimal(str(p.amount)) for p in captured)

    booking.paid_amount = total_paid
    remaining = booking.total_amount - total_paid
    booking.remaining_balance = max(Decimal("0.00"), remaining)

    # Deterministic status transitions — derived from ledger only
    if booking.remaining_balance <= Decimal("0.01"):
        booking.status = BookingStatus.FULLY_PAID
        booking.remaining_balance = Decimal("0.00")
    elif total_paid > Decimal("0.00"):
        booking.status = BookingStatus.PARTIAL_PAID
    else:
        booking.status = BookingStatus.PENDING

    await db.flush()
    return booking
