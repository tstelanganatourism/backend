import httpx
from loguru import logger
from app.core.config import settings
from sqlalchemy import select, func
from datetime import datetime, time
from app.core.timezone import IST
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

class EmailService:
    @staticmethod
    async def send_booking_email(
        recipient_email: str,
        recipient_name: str,
        subject: str,
        html_content: str,
        is_admin: bool = False,
        db: Optional["AsyncSession"] = None,  # Accept existing session to avoid connection pool exhaustion
    ) -> tuple[bool, str]:
        """
        Sends an email using Brevo API with multi-key failover.
        Returns (success: bool, error_reason: str)

        IMPORTANT: Always pass the existing `db` session from the caller (e.g. the ARQ worker).
        Opening a new connection here would double the DB connections per job, causing
        TooManyConnectionsError on Aiven's free-tier 15-connection limit.
        """
        if "STRESS_TEST" in recipient_email:
            return True, ""

        # 1. Determine Primary Key & Sender Email
        if is_admin:
            primary_key = settings.BREVO_API_KEY_ADMIN
            primary_from = settings.BREVO_FROM_EMAIL_ADMIN or settings.BREVO_FROM_EMAIL
            if primary_from and "@gmail.com" in primary_from:
                primary_from = settings.BREVO_FROM_EMAIL
        else:
            primary_key = settings.BREVO_API_KEY_USER
            primary_from = settings.BREVO_FROM_EMAIL_USER or settings.BREVO_FROM_EMAIL

        if not primary_key:
            primary_key = settings.BREVO_API_KEY  # Legacy fallback
            primary_from = settings.BREVO_FROM_EMAIL

        # 2. Smart Brevo Rotation: Check daily sent count using the PASSED-IN session.
        # Never open a new connection here — that causes a deadlock in the ARQ worker.
        try:
            from app.models.booking import EmailLog
            if db is not None:
                tz = IST
                today_start = datetime.combine(datetime.now(tz).date(), time.min).replace(tzinfo=tz)
                query = select(func.count(EmailLog.id)).where(
                    EmailLog.delivery_status == 'SENT',
                    EmailLog.sent_at >= today_start
                )
                result = await db.execute(query)
                today_count = result.scalar() or 0

                if today_count >= 299:
                    logger.warning(f"Daily Brevo limit reached ({today_count} sent). Switching to Backup Key proactively.")
                    primary_key = None  # Force backup key usage
            else:
                # No session provided: skip rotation check (safe fallback, avoids new connection)
                logger.debug("No DB session provided to send_booking_email; skipping daily count check.")
        except Exception as e:
            logger.error(f"Failed to check daily email count: {e}")

        backup_key = settings.BREVO_API_KEY_BACKUP
        backup_from = settings.BREVO_FROM_EMAIL_BACKUP or settings.BREVO_FROM_EMAIL

        if not primary_key and not backup_key:
            return False, "No BREVO_API_KEY configured"

        async def _attempt_send(api_key: str, from_email: str) -> tuple[bool, str]:
            payload = {
                "sender": {"email": from_email, "name": "TS Tourism"},
                "to": [{"email": recipient_email, "name": recipient_name}],
                "subject": subject,
                "htmlContent": html_content,
            }
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        "https://api.brevo.com/v3/smtp/email",
                        json=payload,
                        headers={
                            "api-key": api_key,
                            "Content-Type": "application/json"
                        },
                        timeout=15.0,
                    )
                    if resp.status_code not in (200, 201):
                        return False, f"Brevo API Error: {resp.status_code} - {resp.text}"
                    return True, ""
            except Exception as e:
                return False, f"Exception: {str(e)}"

        # 3. Try Primary Key
        if primary_key:
            success, error_msg = await _attempt_send(primary_key, primary_from)
            if success:
                logger.info(f"Email sent successfully to {recipient_email} using {'Admin' if is_admin else 'User'} key")
                return True, ""

            logger.warning(f"Primary Brevo key failed for {recipient_email}: {error_msg}. Attempting Backup key...")

        # 4. Try Backup Key (Failover)
        if backup_key:
            success, error_msg = await _attempt_send(backup_key, backup_from)
            if success:
                logger.info(f"Email sent successfully to {recipient_email} using Backup key")
                return True, ""

            logger.error(f"Backup Brevo key also failed for {recipient_email}: {error_msg}")
            return False, error_msg

        return False, "Primary key failed and no backup key configured"

email_service = EmailService()
