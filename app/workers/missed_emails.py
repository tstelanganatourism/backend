"""
Missed Emails Recovery Worker

Runs on every ARQ worker startup (run_at_startup=True) and every 15 minutes.
Scans for paid bookings that never had a confirmation email sent (because the
ARQ worker was dead when the payment was processed), and re-queues them.

This is idempotent: it checks the EmailLog table, so it will NEVER send a
duplicate email to a booking that already has a SENT log entry.
"""

from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.db.session import AsyncSessionLocal
from app.core.config import settings


async def recover_missed_emails(ctx):
    """
    On startup and every 15 minutes: find all paid bookings with NO successful
    email log, and re-queue their post-booking email task.
    """
    from app.models.booking import Booking, EmailLog
    from app.models.enums import BookingStatus, DocumentGenerationStatus
    from app.services.pdf_generator import process_post_booking_documents_task

    logger.info("[MissedEmails] Scanning for bookings that never received a confirmation email...")

    async with AsyncSessionLocal() as db:
        # Find bookings that are paid but have ZERO successful "SENT" email log entries.
        # We look back a maximum of 30 days to avoid processing ancient records.
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)

        # Subquery: booking IDs that already have at least one SENT email
        sent_subquery = (
            select(EmailLog.booking_id)
            .where(EmailLog.delivery_status == "SENT")
            .distinct()
            .scalar_subquery()
        )

        # Main query: paid bookings NOT in the sent subquery, created in last 30 days
        stmt = select(Booking).where(
            Booking.status.in_([BookingStatus.FULLY_PAID, BookingStatus.PARTIAL_PAID]),
            Booking.created_at >= cutoff,
            Booking.id.not_in(sent_subquery),
        )

        result = await db.execute(stmt)
        missed_bookings = result.scalars().all()

    if not missed_bookings:
        logger.info("[MissedEmails] All clear — no missed confirmation emails found.")
        return

    logger.warning(f"[MissedEmails] Found {len(missed_bookings)} booking(s) with no confirmation email. Re-sending now...")

    for booking in missed_bookings:
        try:
            is_fully_paid = booking.status == BookingStatus.FULLY_PAID
            logger.info(f"[MissedEmails] Re-sending email for booking {booking.public_id} (fully_paid={is_fully_paid})")

            # Call the task directly in this worker context (no need to re-queue via Redis).
            # This runs immediately and uses ARQ's own retry logic on failure.
            await process_post_booking_documents_task(ctx, booking.id, is_fully_paid=is_fully_paid)

            logger.info(f"[MissedEmails] Successfully sent missed email for {booking.public_id}")
        except Exception as e:
            # Log but don't crash — other bookings should still be processed.
            logger.error(f"[MissedEmails] Failed to send missed email for {booking.public_id}: {e}")

    logger.info(f"[MissedEmails] Recovery complete. Processed {len(missed_bookings)} booking(s).")
