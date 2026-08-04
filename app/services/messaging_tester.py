import asyncio
import httpx
from typing import List, Dict, Any
from loguru import logger

from app.core.config import settings
from app.services.sms_service import (
    send_otp_sms,
    send_booking_confirmation_sms,
    send_room_confirmation_sms,
    send_travel_reminder_sms,
    send_room_reminder_sms,
    TEMPLATES,
)
from app.services.email_service import EmailService

class MessagingTester:
    """Production-grade tester for all MSG91 SMS templates and Brevo email channels."""
    def __init__(self, phone: str, email: str, dry_run: bool = False):
        self.phone = phone
        self.email = email
        self.dry_run = dry_run
        self.results: List[Dict[str, Any]] = []

    def record_result(self, channel: str, test_name: str, status: str, details: str):
        self.results.append({
            "channel": channel,
            "test_name": test_name,
            "status": status,
            "details": details
        })

    async def test_sms_templates(self):
        logger.info("Starting MSG91 SMS template tests...")
        if not settings.MSG91_AUTH_KEY:
            logger.error("MSG91_AUTH_KEY is not set in settings! SMS tests will fail.")
            self.record_result("SMS", "ALL_SMS", "SKIP", "MSG91_AUTH_KEY not configured")
            return

        # 1. OTP SMS
        try:
            if not self.dry_run:
                ok = await send_otp_sms(self.phone, "123456")
                self.record_result("SMS", "TSBOAT_OTP", "PASS" if ok else "FAIL", "OTP sent via MSG91 Flow" if ok else "MSG91 API returned failure")
            else:
                self.record_result("SMS", "TSBOAT_OTP", "DRY_RUN", f"Would send OTP to {self.phone}")
        except Exception as e:
            self.record_result("SMS", "TSBOAT_OTP", "ERROR", str(e))

        # 2. Package Full Payment Confirmation
        try:
            if not self.dry_run:
                ok = await send_booking_confirmation_sms(
                    customer_name="Test User",
                    customer_phone=self.phone,
                    public_id="TSB-TEST-01",
                    package_title="Papikondalu 1 Day Tour",
                    travel_date_str="25-Aug-2026",
                    passenger_count=2,
                    paid_amount="2500.00",
                    total_amount="2500.00",
                    is_partial=False,
                )
                self.record_result("SMS", "TSBOAT_CONFIRMATION_FULL", "PASS" if ok else "FAIL", "Full confirmation SMS sent" if ok else "MSG91 API failure")
            else:
                self.record_result("SMS", "TSBOAT_CONFIRMATION_FULL", "DRY_RUN", f"Would send Full confirmation to {self.phone}")
        except Exception as e:
            self.record_result("SMS", "TSBOAT_CONFIRMATION_FULL", "ERROR", str(e))

        # 3. Package Partial/Advance Confirmation
        try:
            if not self.dry_run:
                ok = await send_booking_confirmation_sms(
                    customer_name="Test User",
                    customer_phone=self.phone,
                    public_id="TSB-TEST-02",
                    package_title="Papikondalu 1 Day Tour",
                    travel_date_str="25-Aug-2026",
                    passenger_count=2,
                    paid_amount="1000.00",
                    total_amount="2500.00",
                    is_partial=True,
                )
                self.record_result("SMS", "TSBOAT_CONFIRMATION_PARTIAL", "PASS" if ok else "FAIL", "Partial confirmation SMS sent" if ok else "MSG91 API failure")
            else:
                self.record_result("SMS", "TSBOAT_CONFIRMATION_PARTIAL", "DRY_RUN", f"Would send Partial confirmation to {self.phone}")
        except Exception as e:
            self.record_result("SMS", "TSBOAT_CONFIRMATION_PARTIAL", "ERROR", str(e))

        # 4. Room Full Payment Confirmation
        try:
            if not self.dry_run:
                ok = await send_room_confirmation_sms(
                    customer_name="Test User",
                    customer_phone=self.phone,
                    public_id="TSR-TEST-01",
                    lodge_name="Haritha Hotel Kolluru",
                    room_name="AC Deluxe Cottage",
                    checkin_date_str="25-Aug-2026",
                    checkin_time_str="11:00 AM",
                    checkout_date_str="26-Aug-2026",
                    checkout_time_str="10:00 AM",
                    paid_amount_str="3500.00",
                    total_amount_str="3500.00",
                    is_partial=False,
                )
                self.record_result("SMS", "TSBOAT_ROOM_CONFIRM", "PASS" if ok else "FAIL", "Room confirmation SMS sent" if ok else "MSG91 API failure")
            else:
                self.record_result("SMS", "TSBOAT_ROOM_CONFIRM", "DRY_RUN", f"Would send Room confirmation to {self.phone}")
        except Exception as e:
            self.record_result("SMS", "TSBOAT_ROOM_CONFIRM", "ERROR", str(e))

        # 5. Room Partial/Advance Confirmation
        try:
            if not self.dry_run:
                ok = await send_room_confirmation_sms(
                    customer_name="Test User",
                    customer_phone=self.phone,
                    public_id="TSR-TEST-02",
                    lodge_name="Haritha Hotel Kolluru",
                    room_name="AC Deluxe Cottage",
                    checkin_date_str="25-Aug-2026",
                    checkin_time_str="11:00 AM",
                    checkout_date_str="26-Aug-2026",
                    checkout_time_str="10:00 AM",
                    paid_amount_str="1500.00",
                    total_amount_str="3500.00",
                    is_partial=True,
                )
                self.record_result("SMS", "TSBOAT_ROOM_CONFIRM_PARTIAL", "PASS" if ok else "FAIL", "Room partial SMS sent" if ok else "MSG91 API failure")
            else:
                self.record_result("SMS", "TSBOAT_ROOM_CONFIRM_PARTIAL", "DRY_RUN", f"Would send Room partial to {self.phone}")
        except Exception as e:
            self.record_result("SMS", "TSBOAT_ROOM_CONFIRM_PARTIAL", "ERROR", str(e))

        # 6. Package Travel Reminder
        try:
            if not self.dry_run:
                ok = await send_travel_reminder_sms(
                    customer_name="Test User",
                    customer_phone=self.phone,
                    public_id="TSB-TEST-01",
                    package_title="Papikondalu 1 Day Tour",
                    boarding_title="Pochavaram Boat Point",
                    boarding_time="07:30 AM",
                    boarding_landmark="Near River Ghat",
                    boarding_phone="9951369573",
                )
                self.record_result("SMS", "TSBOAT_TRAVEL_REMINDER", "PASS" if ok else "FAIL", "Travel reminder SMS sent" if ok else "MSG91 API failure")
            else:
                self.record_result("SMS", "TSBOAT_TRAVEL_REMINDER", "DRY_RUN", f"Would send Travel reminder to {self.phone}")
        except Exception as e:
            self.record_result("SMS", "TSBOAT_TRAVEL_REMINDER", "ERROR", str(e))

        # 7. Room Travel Reminder
        try:
            if not self.dry_run:
                ok = await send_room_reminder_sms(
                    customer_name="Test User",
                    customer_phone=self.phone,
                    public_id="TSR-TEST-01",
                    lodge_name="Haritha Hotel Kolluru",
                    checkin_detail="Check-in at Reception after 11:00 AM",
                )
                self.record_result("SMS", "TSBOAT_ROOM_REMINDER", "PASS" if ok else "FAIL", "Room reminder SMS sent" if ok else "MSG91 API failure")
            else:
                self.record_result("SMS", "TSBOAT_ROOM_REMINDER", "DRY_RUN", f"Would send Room reminder to {self.phone}")
        except Exception as e:
            self.record_result("SMS", "TSBOAT_ROOM_REMINDER", "ERROR", str(e))

    async def test_email_channels(self):
        logger.info("Starting Brevo Email channel tests...")

        # 1. User Key (is_admin=False)
        try:
            if not self.dry_run:
                ok, err = await EmailService.send_booking_email(
                    recipient_email=self.email,
                    recipient_name="Test User",
                    subject="[TEST] TS Boat Tourism - User Notification Channel",
                    html_content="<h2 style='color:#0d6e75;'>User Channel Test</h2><p>This email tests the standard customer notification channel.</p>",
                    is_admin=False,
                )
                self.record_result("EMAIL", "BREVO_USER_CHANNEL", "PASS" if ok else "FAIL", "Email delivered" if ok else err)
            else:
                self.record_result("EMAIL", "BREVO_USER_CHANNEL", "DRY_RUN", f"Would send User email to {self.email}")
        except Exception as e:
            self.record_result("EMAIL", "BREVO_USER_CHANNEL", "ERROR", str(e))

        # 2. Admin Key (is_admin=True)
        try:
            if not self.dry_run:
                ok, err = await EmailService.send_booking_email(
                    recipient_email=self.email,
                    recipient_name="Test Admin",
                    subject="[TEST] TS Boat Tourism - Admin Notification Channel",
                    html_content="<h2 style='color:#0d6e75;'>Admin Channel Test</h2><p>This email tests the admin notification channel (with automatic failover support).</p>",
                    is_admin=True,
                )
                self.record_result("EMAIL", "BREVO_ADMIN_CHANNEL", "PASS" if ok else "FAIL", "Email delivered" if ok else err)
            else:
                self.record_result("EMAIL", "BREVO_ADMIN_CHANNEL", "DRY_RUN", f"Would send Admin email to {self.email}")
        except Exception as e:
            self.record_result("EMAIL", "BREVO_ADMIN_CHANNEL", "ERROR", str(e))

        # 3. Direct Backup Key Test
        try:
            backup_key = settings.BREVO_API_KEY_BACKUP
            backup_from = settings.BREVO_FROM_EMAIL_BACKUP or settings.BREVO_FROM_EMAIL
            if not backup_key:
                self.record_result("EMAIL", "BREVO_BACKUP_KEY_DIRECT", "SKIP", "BREVO_API_KEY_BACKUP not set")
            elif self.dry_run:
                self.record_result("EMAIL", "BREVO_BACKUP_KEY_DIRECT", "DRY_RUN", f"Would test backup key to {self.email}")
            else:
                payload = {
                    "sender": {"email": backup_from, "name": "TS Tourism (Backup Key)"},
                    "to": [{"email": self.email, "name": "Test Backup"}],
                    "subject": "[TEST] TS Boat Tourism - Backup Key Failover Verification",
                    "htmlContent": "<h2 style='color:#0d6e75;'>Backup Key Test</h2><p>This email confirms that the Backup Brevo API Key is valid and working.</p>",
                }
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        "https://api.brevo.com/v3/smtp/email",
                        json=payload,
                        headers={"api-key": backup_key, "Content-Type": "application/json"},
                        timeout=15.0,
                    )
                    if resp.status_code in [200, 201, 202]:
                        self.record_result("EMAIL", "BREVO_BACKUP_KEY_DIRECT", "PASS", f"Delivered: {resp.status_code}")
                    else:
                        self.record_result("EMAIL", "BREVO_BACKUP_KEY_DIRECT", "FAIL", f"HTTP {resp.status_code} - {resp.text}")
        except Exception as e:
            self.record_result("EMAIL", "BREVO_BACKUP_KEY_DIRECT", "ERROR", str(e))
