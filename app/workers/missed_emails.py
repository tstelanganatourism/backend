"""
Missed Emails Recovery Worker

Runs on every ARQ worker startup (run_at_startup=True) and every 15 minutes.
Scans for paid bookings that are missing either:
  - A customer confirmation email (FULL_PAYMENT or PARTIAL_PAYMENT with SENT status)
  - An admin notification email (ADMIN_NOTIFICATION with SENT status)

This handles both full misses (worker was dead) and partial failures
(customer email sent but admin failed, or vice versa).

This is fully idempotent: process_post_booking_documents_task internally checks
the EmailLog before sending, so it will NEVER send a duplicate email.
"""

from loguru import logger
from sqlalchemy import select, exists
from datetime import datetime, timedelta, timezone

from app.db.session import AsyncSessionLocal
from app.core.config import settings


async def recover_missed_emails(ctx):
    """
    On startup and every 15 minutes: find all paid bookings that are missing
    their customer confirmation email OR their admin notification email, and
    re-trigger the post-booking task for each one (which is idempotent).
    """
    from app.models.booking import Booking, EmailLog
    from app.models.enums import BookingStatus
    from app.services.pdf_generator import process_post_booking_documents_task

    logger.info("[MissedEmails] Scanning for bookings missing confirmation or admin emails...")

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    async with AsyncSessionLocal() as db:
        # Subquery: booking IDs that have a SENT or SKIPPED customer email
        sent_customer_sq = (
            select(EmailLog.booking_id)
            .where(
                EmailLog.delivery_status.in_(["SENT", "SKIPPED"]),
                EmailLog.email_type.in_(["FULL_PAYMENT", "PARTIAL_PAYMENT"])
            )
            .distinct()
            .scalar_subquery()
        )

        # Subquery: booking IDs that have a SENT admin notification
        sent_admin_sq = (
            select(EmailLog.booking_id)
            .where(
                EmailLog.delivery_status == "SENT",
                EmailLog.email_type == "ADMIN_NOTIFICATION"
            )
            .distinct()
            .scalar_subquery()
        )

        # Find bookings missing customer email OR admin notification
        stmt = (
            select(Booking)
            .where(
                Booking.status.in_([BookingStatus.FULLY_PAID, BookingStatus.PARTIAL_PAID]),
                Booking.created_at >= cutoff,
                # Missing customer email OR missing admin notification
                (
                    Booking.id.not_in(sent_customer_sq) |
                    Booking.id.not_in(sent_admin_sq)
                ),
            )
        )

        result = await db.execute(stmt)
        missed_bookings = result.scalars().all()

    if not missed_bookings:
        logger.info("[MissedEmails] All clear -- no missed emails found.")
        return

    logger.warning(
        f"[MissedEmails] Found {len(missed_bookings)} booking(s) with missing emails. "
        f"Re-triggering now..."
    )

    for booking in missed_bookings:
        try:
            is_fully_paid = booking.status == BookingStatus.FULLY_PAID
            logger.info(
                f"[MissedEmails] Re-sending for {booking.public_id} "
                f"(fully_paid={is_fully_paid})"
            )
            # process_post_booking_documents_task is idempotent:
            # it checks EmailLog before sending each email, so nothing is duplicated.
            await process_post_booking_documents_task(
                ctx, booking.id, is_fully_paid=is_fully_paid
            )
            logger.info(f"[MissedEmails] Successfully re-sent for {booking.public_id}")
        except Exception as e:
            # Log but continue — one failure must not block the others
            logger.error(
                f"[MissedEmails] Failed to re-send for {booking.public_id}: {e}"
            )

    logger.info(
        f"[MissedEmails] Recovery complete. Processed {len(missed_bookings)} booking(s)."
    )
