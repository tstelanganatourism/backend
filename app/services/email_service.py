import httpx
from loguru import logger
from app.core.config import settings

class EmailService:
    @staticmethod
    async def send_booking_email(
        recipient_email: str,
        recipient_name: str,
        subject: str,
        html_content: str
    ) -> tuple[bool, str]:
        """
        Sends an email using Brevo API.
        Returns (success: bool, error_reason: str)
        """
        if "STRESS_TEST" in recipient_email:
            return True, ""
            
        if not settings.BREVO_API_KEY:
            return False, "BREVO_API_KEY not configured"

        payload = {
            "sender": {"email": settings.BREVO_FROM_EMAIL, "name": "TS Tourism"},
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
                        "api-key": settings.BREVO_API_KEY,
                        "Content-Type": "application/json"
                    },
                    timeout=15.0,
                )
                if resp.status_code not in (200, 201):
                    error_msg = f"Brevo API Error: {resp.status_code} - {resp.text}"
                    logger.error(error_msg)
                    return False, error_msg
                logger.info(f"Email sent successfully to {recipient_email}")
                return True, ""
        except Exception as e:
            error_msg = f"Failed to send email: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

email_service = EmailService()
