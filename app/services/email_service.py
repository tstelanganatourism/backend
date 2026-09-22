import httpx
import asyncio
import aiosmtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from loguru import logger
from app.core.config import settings
from sqlalchemy import select, func
from datetime import datetime, time
from app.core.timezone import IST
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _send_via_gmail_smtp(
    recipient_email: str,
    recipient_name: str,
    subject: str,
    html_content: str,
) -> tuple[bool, str]:
    """
    Send email via Gmail SMTP using an App Password.
    Gmail → Gmail/any address: 100% deliverability, no sender domain issues.
    Requires GMAIL_USER and GMAIL_APP_PASSWORD set in env.
    """
    gmail_user = settings.GMAIL_USER
    gmail_pass = settings.GMAIL_APP_PASSWORD

    if not gmail_user or not gmail_pass:
        return False, "GMAIL_USER or GMAIL_APP_PASSWORD not configured"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"TS Boat Tourism <{gmail_user}>"
        msg["To"] = f"{recipient_name} <{recipient_email}>"
        msg["Reply-To"] = gmail_user
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        await aiosmtplib.send(
            msg,
            hostname="smtp.gmail.com",
            port=587,
            start_tls=True,
            username=gmail_user,
            password=gmail_pass,
            timeout=20,
        )
        logger.info(f"Gmail SMTP: email sent to {recipient_email}")
        return True, ""
    except Exception as e:
        logger.error(f"Gmail SMTP failed for {recipient_email}: {e}")
        return False, str(e)


async def _send_via_brevo(
    api_key: str,
    from_email: str,
    recipient_email: str,
    recipient_name: str,
    subject: str,
    html_content: str,
    reply_to: Optional[str] = None,
) -> tuple[bool, str]:
    """Send email via Brevo transactional API."""
    payload: dict = {
        "sender": {"email": from_email, "name": "TS Tourism"},
        "to": [{"email": recipient_email, "name": recipient_name}],
        "subject": subject,
        "htmlContent": html_content,
    }
    # Always add replyTo so replies go to the real Gmail inbox
    reply_addr = reply_to or settings.GMAIL_USER or from_email
    if reply_addr:
        payload["replyTo"] = {"email": reply_addr, "name": "TS Boat Tourism"}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={
                    "api-key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )
            if resp.status_code not in (200, 201):
                return False, f"Brevo API Error: {resp.status_code} - {resp.text}"
            return True, ""
    except Exception as e:
        return False, f"Exception: {str(e)}"


class EmailService:
    @staticmethod
    async def send_booking_email(
        recipient_email: str,
        recipient_name: str,
        subject: str,
        html_content: str,
        is_admin: bool = False,
        db: Optional["AsyncSession"] = None,
    ) -> tuple[bool, str]:
        """
        Send transactional email with a 3-tier delivery chain:
          1. Gmail SMTP (primary — 100% deliverability, no domain issues)
          2. Brevo primary key
          3. Brevo backup key

        Returns (success: bool, error_reason: str)

        IMPORTANT: Always pass the existing `db` session from the caller.
        Opening a new connection here would double the DB connections per job, causing
        TooManyConnectionsError on Aiven's free-tier 15-connection limit.
        """
        if "STRESS_TEST" in recipient_email:
            return True, ""

        # ── Tier 1: Gmail SMTP ───────────────────────────────────────────────
        if settings.GMAIL_USER and settings.GMAIL_APP_PASSWORD:
            success, error = await _send_via_gmail_smtp(
                recipient_email, recipient_name, subject, html_content
            )
            if success:
                return True, ""
            logger.warning(f"Gmail SMTP failed, falling back to Brevo: {error}")

        # ── Determine Brevo keys & sender ────────────────────────────────────
        if is_admin:
            primary_key = settings.BREVO_API_KEY_ADMIN
            primary_from = settings.BREVO_FROM_EMAIL_ADMIN or settings.BREVO_FROM_EMAIL
        else:
            primary_key = settings.BREVO_API_KEY_USER
            primary_from = settings.BREVO_FROM_EMAIL_USER or settings.BREVO_FROM_EMAIL

        if not primary_from:
            primary_from = "bookings@tstelanganatourism.com"

        # ── Daily quota guard ────────────────────────────────────────────────
        try:
            from app.models.booking import EmailLog
            if db is not None:
                tz = IST
                today_start = datetime.combine(datetime.now(tz).date(), time.min).replace(tzinfo=tz)
                query = select(func.count(EmailLog.id)).where(
                    EmailLog.delivery_status == "SENT",
                    EmailLog.sent_at >= today_start,
                )
                result = await db.execute(query)
                today_count = result.scalar() or 0
                if today_count >= 299:
                    logger.warning(f"Daily Brevo limit reached ({today_count} sent). Using Backup Key.")
                    primary_key = None
            else:
                logger.debug("No DB session provided; skipping daily count check.")
        except Exception as e:
            logger.error(f"Failed to check daily email count: {e}")

        backup_key = settings.BREVO_API_KEY_BACKUP
        backup_from = settings.BREVO_FROM_EMAIL_BACKUP or settings.BREVO_FROM_EMAIL

        if not primary_key and not backup_key:
            return False, "No BREVO_API_KEY configured and Gmail SMTP not available"

        # ── Tier 2: Brevo primary key ────────────────────────────────────────
        if primary_key:
            success, error_msg = await _send_via_brevo(
                primary_key, primary_from, recipient_email, recipient_name, subject, html_content
            )
            if success:
                logger.info(f"Brevo primary key: email sent to {recipient_email}")
                return True, ""
            logger.warning(f"Brevo primary key failed for {recipient_email}: {error_msg}. Trying backup...")

        # ── Tier 3: Brevo backup key ─────────────────────────────────────────
        if backup_key:
            success, error_msg = await _send_via_brevo(
                backup_key, backup_from, recipient_email, recipient_name, subject, html_content
            )
            if success:
                logger.info(f"Brevo backup key: email sent to {recipient_email}")
                return True, ""
            logger.error(f"Brevo backup key also failed for {recipient_email}: {error_msg}")
            return False, error_msg

        return False, "All email delivery methods failed"


email_service = EmailService()
