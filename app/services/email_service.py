import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, TYPE_CHECKING
from datetime import datetime, time

import httpx
from loguru import logger
from sqlalchemy import select, func

from app.core.config import settings
from app.core.timezone import IST

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _sync_send_gmail(
    gmail_user: str,
    gmail_pass: str,
    recipient_email: str,
    recipient_name: str,
    subject: str,
    html_content: str,
):
    """Synchronous Gmail SSL SMTP send helper run via asyncio.to_thread."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"TS Boat Tourism <{gmail_user}>"
    msg["To"] = f"{recipient_name} <{recipient_email}>" if recipient_name else recipient_email
    msg["Reply-To"] = gmail_user
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    clean_pass = gmail_pass.replace(" ", "")
    server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15)
    server.login(gmail_user, clean_pass)
    server.sendmail(gmail_user, [recipient_email], msg.as_string())
    server.quit()
    return True


async def _send_via_gmail_smtp(
    recipient_email: str,
    recipient_name: str,
    subject: str,
    html_content: str,
) -> tuple[bool, str]:
    """
    Send email via Gmail SMTP using Google App Password.
    Zero external dependencies (uses built-in smtplib via thread pool).
    100% deliverability — sent natively through Google's own mail servers.
    """
    gmail_user = settings.GMAIL_USER
    gmail_pass = settings.GMAIL_APP_PASSWORD

    if not gmail_user or not gmail_pass:
        return False, "GMAIL_USER or GMAIL_APP_PASSWORD not configured"

    try:
        await asyncio.to_thread(
            _sync_send_gmail,
            gmail_user,
            gmail_pass,
            recipient_email,
            recipient_name,
            subject,
            html_content,
        )
        logger.info(f"Gmail SMTP: sent email to {recipient_email}")
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
) -> tuple[bool, str]:
    """Send email via Brevo transactional API."""
    # Ensure sender is valid in Brevo (must not be unverified custom domain)
    if not from_email or "tstelanganatourism.com" in from_email:
        from_email = "tstelanganatourism@gmail.com"

    payload = {
        "sender": {"email": from_email, "name": "TS Boat Tourism"},
        "to": [{"email": recipient_email, "name": recipient_name or recipient_email}],
        "subject": subject,
        "htmlContent": html_content,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={
                    "api-key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            if resp.status_code not in (200, 201):
                logger.error(f"Brevo API error: {resp.status_code} - {resp.text}")
                return False, f"Brevo Error: {resp.status_code} - {resp.text}"
            return True, ""
    except Exception as e:
        logger.error(f"Brevo exception: {e}")
        return False, f"Brevo Exception: {str(e)}"


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
        Sends email with a robust multi-tier delivery chain:
          Tier 1: Gmail SMTP  — primary, 100% deliverability, 500/day free
          Tier 2: Brevo primary key  — automatic fallback
          Tier 3: Brevo backup key   — last resort failover

        Always reuses the passed-in db session to prevent connection pool exhaustion.
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
            logger.warning(f"Gmail SMTP failed for {recipient_email}: {error}. Falling back to Brevo...")

        # ── Tier 2 & 3: Brevo Setup ──────────────────────────────────────────
        if is_admin:
            primary_key = settings.BREVO_API_KEY_ADMIN or settings.BREVO_API_KEY
            primary_from = settings.BREVO_FROM_EMAIL_ADMIN or settings.BREVO_FROM_EMAIL
        else:
            primary_key = settings.BREVO_API_KEY_USER or settings.BREVO_API_KEY
            primary_from = settings.BREVO_FROM_EMAIL_USER or settings.BREVO_FROM_EMAIL

        if not primary_from or "tstelanganatourism.com" in primary_from:
            primary_from = "tstelanganatourism@gmail.com"

        # Daily quota guard
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
        except Exception as e:
            logger.error(f"Failed to check daily email count: {e}")

        backup_key = settings.BREVO_API_KEY_BACKUP
        backup_from = settings.BREVO_FROM_EMAIL_BACKUP or settings.BREVO_FROM_EMAIL or "tstelanganatourism@gmail.com"

        if not primary_key and not backup_key:
            return False, "No email credentials configured (neither Gmail SMTP nor Brevo)"

        # ── Tier 2: Brevo Primary ────────────────────────────────────────────
        if primary_key:
            success, error_msg = await _send_via_brevo(
                primary_key, primary_from, recipient_email, recipient_name, subject, html_content
            )
            if success:
                logger.info(f"Brevo primary sent email to {recipient_email}")
                return True, ""
            logger.warning(f"Brevo primary failed for {recipient_email}: {error_msg}. Trying backup...")

        # ── Tier 3: Brevo Backup ─────────────────────────────────────────────
        if backup_key:
            success, error_msg = await _send_via_brevo(
                backup_key, backup_from, recipient_email, recipient_name, subject, html_content
            )
            if success:
                logger.info(f"Brevo backup sent email to {recipient_email}")
                return True, ""
            logger.error(f"Brevo backup also failed for {recipient_email}: {error_msg}")
            return False, error_msg

        return False, "All email delivery tiers failed"


email_service = EmailService()
