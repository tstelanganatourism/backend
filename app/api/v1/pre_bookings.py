"""
Pre-Booking Public API
Handles submission from /prebooking page and sends emails.
"""
import uuid
import httpx
from datetime import date, datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status, BackgroundTasks
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.pre_booking import PreBooking
from app.core.config import settings
from app.core.timezone import IST
from loguru import logger

router = APIRouter(tags=["Pre-Bookings - Public"])

# ── In-memory rate limiter ─────────────────────────────────────────────────────
_rate_map: dict[str, dict] = {}

def _check_rate(ip: str, max_per_hour: int = 30) -> bool:
    if ip in ("127.0.0.1", "localhost", "::1", "testclient"):
        return True
    now = datetime.now().timestamp()
    record = _rate_map.get(ip)
    if not record or now > record["reset_at"]:
        _rate_map[ip] = {"count": 1, "reset_at": now + 3600}
        return True
    if record["count"] >= max_per_hour:
        return False
    record["count"] += 1
    return True


# ── Schemas ────────────────────────────────────────────────────────────────────

class PreBookingCreate(BaseModel):
    package_id: str = Field(..., min_length=1, max_length=100)
    package_name: str = Field(..., min_length=1, max_length=255)
    travel_date: date
    adult_count: int = Field(default=1, ge=1, le=50)
    child_count: int = Field(default=0, ge=0, le=50)
    customer_name: str = Field(..., min_length=2, max_length=255)
    customer_email: EmailStr
    customer_phone: str = Field(..., min_length=10, max_length=20)
    notes: Optional[str] = Field(None, max_length=1000)


class PreBookingResponse(BaseModel):
    pnr_number: str
    ref_id: str
    message: str
    whatsapp_url: str


# ── Email helpers ──────────────────────────────────────────────────────────────

async def _send_brevo(to_email: str, to_name: str, subject: str, html: str):
    """Fire-and-forget Brevo send."""
    api_key = settings.BREVO_API_KEY_USER or settings.BREVO_API_KEY or ""
    from_email = settings.BREVO_FROM_EMAIL_USER or settings.BREVO_FROM_EMAIL or "tstelanganatourism@gmail.com"
    if not api_key:
        logger.warning("No Brevo API key configured — skipping email")
        return False
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json={
                    "sender": {"email": from_email, "name": "TS Boat Tourism"},
                    "to": [{"email": to_email, "name": to_name}],
                    "subject": subject,
                    "htmlContent": html,
                },
                headers={"api-key": api_key, "Content-Type": "application/json"},
            )
            if resp.status_code not in (200, 201):
                logger.error(f"Brevo error {resp.status_code}: {resp.text}")
                return False
            return True
    except Exception as e:
        logger.error(f"Email send exception: {e}")
        return False


async def _dispatch_prebooking_emails(pb_id: int):
    """Background task to send customer and admin confirmation emails via Brevo asynchronously."""
    from app.db.session import AsyncSessionLocal
    from sqlalchemy import select
    import asyncio
    try:
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(PreBooking).where(PreBooking.id == pb_id))
            pb = res.scalar_one_or_none()
            if not pb:
                return

            user_html = _user_email_html(pb)
            admin_html = _admin_email_html(pb)

            user_task = _send_brevo(
                pb.customer_email, pb.customer_name,
                f"✅ Pre-Booking Confirmed [PNR: {pb.ref_id}] — {pb.package_name} | TS Boat Tourism",
                user_html,
            )
            admin_task = _send_brevo(
                "tstelanganatourism@gmail.com", "TS Boat Tourism Admin",
                f"🆕 New Pre-Booking Lead [PNR: {pb.ref_id}] — {pb.customer_name} | {pb.package_name}",
                admin_html,
            )
            user_ok, admin_ok = await asyncio.gather(user_task, admin_task)

            pb.user_email_sent = user_ok
            pb.admin_email_sent = admin_ok
            await session.commit()
            logger.info(f"Dispatched pre-booking emails for PNR {pb.ref_id}: user={user_ok}, admin={admin_ok}")
    except Exception as e:
        logger.error(f"Error in _dispatch_prebooking_emails for {pb_id}: {e}")


def _user_email_html(pb: PreBooking) -> str:
    travel = pb.travel_date.strftime("%A, %d %B %Y") if pb.travel_date else "—"
    pax = f"{pb.adult_count} Adult{'s' if pb.adult_count != 1 else ''}"
    if pb.child_count:
        pax += f" + {pb.child_count} Child{'ren' if pb.child_count > 1 else ''}"

    import re
    clean_phone = re.sub(r'\D', '', pb.customer_phone or "")
    if clean_phone.startswith('91') and len(clean_phone) > 10:
        clean_phone = clean_phone[2:]
    formatted_phone = f"+91 {clean_phone}" if len(clean_phone) == 10 else (pb.customer_phone or "")
    wa_phone = f"91{clean_phone}" if len(clean_phone) == 10 else clean_phone

    import urllib.parse
    wa_msg = (
        f"Hello TS Boat Tourism! I have pre-booked the *{pb.package_name}* "
        f"for *{travel}* ({pax}). My PNR Number is *{pb.ref_id}*. Please confirm my slot."
    )
    wa_url = f"https://wa.me/919951369573?text={urllib.parse.quote(wa_msg)}"

    notes_html = ""
    if pb.notes:
        notes_html = f"""
        <tr>
          <td style="padding:12px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6; vertical-align:top;">Special Requests</td>
          <td style="padding:12px 18px; font-size:13px; color:#0F3D56; font-weight:600; text-align:right; border-bottom:1px solid #eef2f6;">{pb.notes}</td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pre-Booking Confirmed [PNR: {pb.ref_id}] — TS Boat Tourism</title>
</head>
<body style="margin:0; padding:0; background-color:#F4F6F8; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing:antialiased;">
<table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#F4F6F8; margin:0; padding:24px 12px;">
  <tr>
    <td align="center">
      <table role="presentation" width="600" border="0" cellpadding="0" cellspacing="0" style="width:100%; max-width:600px; background-color:#ffffff; border-radius:16px; border:1px solid #E2E8F0; overflow:hidden; box-shadow:0 4px 16px rgba(15, 61, 86, 0.05);">
        
        <!-- BRAND HEADER -->
        <tr>
          <td align="center" style="background-color:#0F3D56; padding:32px 24px 28px 24px; border-bottom:3px solid #1598a1;">
            <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin:0 auto 12px auto;">
              <tr>
                <td align="center">
                  <img src="https://res.cloudinary.com/r929tquv/image/upload/v1784630155/tsboat_logo_apple_touch.jpg" width="70" height="70" alt="TS Boat Tourism" style="display:block; width:70px; height:70px; border-radius:50%; border:2px solid rgba(255,255,255,0.25); margin:0 auto; object-fit:cover;">
                </td>
              </tr>
            </table>
            <div style="color:#ffffff; font-size:22px; line-height:28px; font-weight:900; letter-spacing:-0.3px; margin:0 0 3px 0;">
              TS Boat Tourism
            </div>
            <div style="color:#4dd9e4; font-size:11px; line-height:16px; font-weight:700; letter-spacing:1px; text-transform:uppercase;">
              Official Booking Portal · Govt. Authorized
            </div>
          </td>
        </tr>

        <!-- BODY CONTENT -->
        <tr>
          <td style="padding:32px 28px 24px 28px; color:#1e293b;">

            <!-- PNR CONFIRMATION BANNER -->
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#F0FAF9; border:1px solid #99F6E4; border-radius:12px; margin-bottom:24px;">
              <tr>
                <td style="padding:20px 24px; text-align:center;">
                  <div style="display:inline-block; background-color:#1598a1; color:#ffffff; font-size:10px; font-weight:800; letter-spacing:1px; text-transform:uppercase; padding:3px 10px; border-radius:20px; margin-bottom:8px;">
                    Early Pre-Booking Confirmed ✓
                  </div>
                  <div style="color:#0F3D56; font-size:18px; font-weight:800; margin:0 0 6px 0;">
                    Your Travel Slot is Reserved!
                  </div>
                  <div style="color:#64748b; font-size:12px; margin-bottom:10px;">
                    Keep this PNR number handy for all communications:
                  </div>
                  <div style="display:inline-block; background-color:#ffffff; border:1.5px solid #1598a1; color:#0F3D56; font-size:24px; font-weight:900; letter-spacing:2px; font-family:Courier, monospace; padding:8px 22px; border-radius:10px;">
                    {pb.ref_id}
                  </div>
                  <div style="color:#059669; font-size:11px; font-weight:700; margin-top:10px;">
                    🔒 100% Free Reservation · No Advance Payment Required
                  </div>
                </td>
              </tr>
            </table>

            <!-- PERSONAL GREETING -->
            <p style="margin:0 0 10px 0; color:#0F3D56; font-size:16px; font-weight:700;">
              Dear {pb.customer_name},
            </p>
            <p style="margin:0 0 24px 0; color:#475569; font-size:14px; line-height:22px;">
              Thank you for pre-booking with <strong>TS Boat Tourism</strong>! Your early reservation request for <strong style="color:#0F3D56;">{pb.package_name}</strong> has been received and registered into our system. Our team will contact you within <strong>24 hours</strong> at <strong>{formatted_phone}</strong> to confirm your slot and finalize your trip details.
            </p>

            <!-- BOOKING SUMMARY TABLE (GMAIL COMPATIBLE TABLE LAYOUT) -->
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="border:1px solid #E2E8F0; border-radius:12px; overflow:hidden; margin-bottom:24px; border-collapse:collapse;">
              <tr style="background-color:#F8FAFC;">
                <td colspan="2" style="padding:12px 18px; border-bottom:1px solid #E2E8F0; font-size:11px; font-weight:800; color:#475569; letter-spacing:1px; text-transform:uppercase;">
                  📋 Pre-Booking Summary
                </td>
              </tr>
              <tr>
                <td style="padding:12px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6; width:38%;">PNR Number</td>
                <td style="padding:12px 18px; font-size:13px; color:#1598a1; font-weight:900; text-align:right; border-bottom:1px solid #eef2f6; font-family:Courier, monospace; letter-spacing:1px;">{pb.ref_id}</td>
              </tr>
              <tr>
                <td style="padding:12px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Selected Package</td>
                <td style="padding:12px 18px; font-size:13px; color:#0F3D56; font-weight:700; text-align:right; border-bottom:1px solid #eef2f6;">{pb.package_name}</td>
              </tr>
              <tr>
                <td style="padding:12px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Travel Date</td>
                <td style="padding:12px 18px; font-size:13px; color:#0F3D56; font-weight:700; text-align:right; border-bottom:1px solid #eef2f6;">{travel}</td>
              </tr>
              <tr>
                <td style="padding:12px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Total Travellers</td>
                <td style="padding:12px 18px; font-size:13px; color:#0F3D56; font-weight:700; text-align:right; border-bottom:1px solid #eef2f6;">{pax}</td>
              </tr>
              <tr>
                <td style="padding:12px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Contact Phone</td>
                <td style="padding:12px 18px; font-size:13px; color:#0F3D56; font-weight:700; text-align:right; border-bottom:1px solid #eef2f6;">{formatted_phone}</td>
              </tr>
              <tr>
                <td style="padding:12px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Customer Email</td>
                <td style="padding:12px 18px; font-size:13px; color:#0F3D56; font-weight:700; text-align:right; border-bottom:1px solid #eef2f6;">{pb.customer_email}</td>
              </tr>
              {notes_html}
              <tr>
                <td style="padding:12px 18px; font-size:13px; color:#64748b;">Reservation Status</td>
                <td style="padding:12px 18px; font-size:13px; color:#059669; font-weight:800; text-align:right;">✓ Registered · Awaiting Confirmation</td>
              </tr>
            </table>

            <!-- WHAT HAPPENS NEXT -->
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#F8FAFC; border:1px solid #E2E8F0; border-radius:12px; margin-bottom:24px;">
              <tr>
                <td style="padding:16px 20px 10px 20px; font-size:12px; font-weight:800; color:#0F3D56; letter-spacing:0.8px; text-transform:uppercase;">
                  ⚡ What Happens Next?
                </td>
              </tr>
              <tr>
                <td style="padding:8px 20px 16px 20px;">
                  <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
                    <tr>
                      <td valign="top" style="width:28px; padding-right:12px; padding-top:2px;">
                        <div style="width:22px; height:22px; background-color:#1598a1; color:#ffffff; font-size:11px; font-weight:800; text-align:center; line-height:22px; border-radius:50%;">1</div>
                      </td>
                      <td style="padding-bottom:12px;">
                        <div style="font-size:13px; font-weight:700; color:#0F3D56;">Slot Verification</div>
                        <div style="font-size:12px; color:#64748b; line-height:18px;">Our logistics team checks boat cruise and bamboo hut availability for {travel}.</div>
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" style="width:28px; padding-right:12px; padding-top:2px;">
                        <div style="width:22px; height:22px; background-color:#1598a1; color:#ffffff; font-size:11px; font-weight:800; text-align:center; line-height:22px; border-radius:50%;">2</div>
                      </td>
                      <td style="padding-bottom:12px;">
                        <div style="font-size:13px; font-weight:700; color:#0F3D56;">Our Team Calls or WhatsApps You</div>
                        <div style="font-size:12px; color:#64748b; line-height:18px;">We contact you at {formatted_phone} within 24 hours to confirm group numbers and pickup.</div>
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" style="width:28px; padding-right:12px; padding-top:2px;">
                        <div style="width:22px; height:22px; background-color:#1598a1; color:#ffffff; font-size:11px; font-weight:800; text-align:center; line-height:22px; border-radius:50%;">3</div>
                      </td>
                      <td style="padding-bottom:4px;">
                        <div style="font-size:13px; font-weight:700; color:#0F3D56;">Tickets & Travel Guide Delivered</div>
                        <div style="font-size:12px; color:#64748b; line-height:18px;">Receive your confirmed booking tickets and itinerary details directly on your phone and email.</div>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>

            <!-- WHATSAPP BUTTON -->
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
              <tr>
                <td align="center">
                  <a href="{wa_url}" target="_blank" style="display:block; width:100%; box-sizing:border-box; background-color:#25D366; color:#ffffff; font-size:14px; font-weight:800; text-align:center; text-decoration:none; padding:15px 20px; border-radius:10px; letter-spacing:0.3px;">
                    💬 Chat Directly with Our Team on WhatsApp
                  </a>
                </td>
              </tr>
            </table>

            <!-- REAL OFFICE DETAILS & HELPLINES -->
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#F0FAF9; border:1px solid #CCFBF1; border-radius:12px; margin-bottom:16px;">
              <tr>
                <td style="padding:18px 20px;">
                  <div style="font-size:11px; font-weight:800; color:#0F3D56; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:8px;">
                    📍 Official Head Booking Office
                  </div>
                  <div style="font-size:13px; color:#0F3D56; font-weight:800; line-height:18px;">
                    TS Boat Tourism (Bhadrachalam Office)
                  </div>
                  <div style="font-size:12px; color:#475569; line-height:18px; margin-top:4px;">
                    Door No. 10-1-2/1, Ground Floor, Om Shanthi Building Sataram,<br>
                    Kalyana Mandapam Road, Bhadrachalam, Telangana — 507111, India
                  </div>
                  <div style="border-top:1px solid #D1FAE5; margin:12px 0;"></div>
                  <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="font-size:12px; color:#475569; padding:3px 0;">
                        📞 <strong>Helplines:</strong> <a href="tel:+919951369573" style="color:#1598a1; text-decoration:none; font-weight:700;">+91 99513 69573</a> &nbsp;·&nbsp; <a href="tel:+917780119268" style="color:#1598a1; text-decoration:none; font-weight:700;">+91 77801 19268</a>
                      </td>
                    </tr>
                    <tr>
                      <td style="font-size:12px; color:#475569; padding:3px 0;">
                        ✉️ <strong>Official Email:</strong> <a href="mailto:tstelanganatourism@gmail.com" style="color:#1598a1; text-decoration:none; font-weight:600;">tstelanganatourism@gmail.com</a>
                      </td>
                    </tr>
                    <tr>
                      <td style="font-size:12px; color:#475569; padding:3px 0;">
                        🕒 <strong>Office Hours:</strong> 7:00 AM – 9:00 PM IST (Open All 7 Days)
                      </td>
                    </tr>
                    <tr>
                      <td style="font-size:12px; color:#475569; padding:3px 0;">
                        ⛵ <strong>Cruise Reporting:</strong> 7:00 AM – 7:30 AM IST (Bhadrachalam / Pochavaram Dock)
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>

          </td>
        </tr>

        <!-- FOOTER -->
        <tr>
          <td align="center" style="background-color:#F8FAFC; border-top:1px solid #E2E8F0; padding:20px 24px;">
            <div style="font-size:11px; color:#94a3b8; line-height:18px;">
              © 2026 TS Boat Tourism. All rights reserved.<br>
              Official Telangana & Papikondalu Tourism Portal · <a href="https://tstelanganatourism.com" target="_blank" style="color:#1598a1; text-decoration:none; font-weight:600;">tstelanganatourism.com</a>
            </div>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _admin_email_html(pb: PreBooking) -> str:
    travel = pb.travel_date.strftime("%A, %d %B %Y") if pb.travel_date else "—"
    submitted_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    pax = f"{pb.adult_count} Adult{'s' if pb.adult_count != 1 else ''}"
    if pb.child_count:
        pax += f" + {pb.child_count} Child{'ren' if pb.child_count > 1 else ''}"

    import re
    clean_phone = re.sub(r'\D', '', pb.customer_phone or "")
    if clean_phone.startswith('91') and len(clean_phone) > 10:
        clean_phone = clean_phone[2:]
    formatted_phone = f"+91 {clean_phone}" if len(clean_phone) == 10 else (pb.customer_phone or "")
    wa_phone = f"91{clean_phone}" if len(clean_phone) == 10 else clean_phone

    import urllib.parse
    wa_msg = (
        f"Hello {pb.customer_name}! This is TS Boat Tourism team. "
        f"We received your pre-booking PNR {pb.ref_id} for {pb.package_name} on {travel}. "
        f"We would like to confirm your slot. Are you available to discuss?"
    )
    wa_url = f"https://wa.me/{wa_phone}?text={urllib.parse.quote(wa_msg)}"

    notes_html = ""
    if pb.notes:
        notes_html = f"""
        <tr>
          <td style="padding:11px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6; vertical-align:top;">Customer Notes</td>
          <td style="padding:11px 18px; font-size:13px; color:#0F3D56; font-weight:600; text-align:right; border-bottom:1px solid #eef2f6;">{pb.notes}</td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>New Pre-Booking Lead [PNR: {pb.ref_id}] — {pb.customer_name}</title>
</head>
<body style="margin:0; padding:0; background-color:#F4F6F8; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing:antialiased;">
<table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#F4F6F8; margin:0; padding:24px 12px;">
  <tr>
    <td align="center">
      <table role="presentation" width="600" border="0" cellpadding="0" cellspacing="0" style="width:100%; max-width:600px; background-color:#ffffff; border-radius:16px; border:1px solid #E2E8F0; overflow:hidden; box-shadow:0 4px 16px rgba(15, 61, 86, 0.05);">

        <!-- ADMIN BRAND HEADER -->
        <tr>
          <td align="center" style="background-color:#0F3D56; padding:28px 24px 24px 24px; border-bottom:3px solid #1598a1;">
            <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin:0 auto 10px auto;">
              <tr>
                <td align="center">
                  <img src="https://res.cloudinary.com/r929tquv/image/upload/v1784630155/tsboat_logo_apple_touch.jpg" width="64" height="64" alt="TS Boat Tourism" style="display:block; width:64px; height:64px; border-radius:50%; border:2px solid rgba(255,255,255,0.25); margin:0 auto; object-fit:cover;">
                </td>
              </tr>
            </table>
            <div style="color:#ffffff; font-size:20px; line-height:26px; font-weight:900; letter-spacing:-0.3px; margin:0 0 3px 0;">
              TS Boat Tourism · Admin Alert
            </div>
            <div style="color:#4dd9e4; font-size:11px; line-height:16px; font-weight:700; letter-spacing:1px; text-transform:uppercase;">
              🆕 New Early Pre-Booking Lead Received
            </div>
          </td>
        </tr>

        <!-- BODY CONTENT -->
        <tr>
          <td style="padding:28px 26px 20px 26px; color:#1e293b;">

            <!-- PNR HIGHLIGHT BOX -->
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#F0FAF9; border:1px solid #99F6E4; border-radius:12px; margin-bottom:20px;">
              <tr>
                <td style="padding:16px 20px; text-align:center;">
                  <div style="font-size:11px; font-weight:800; color:#1598a1; text-transform:uppercase; letter-spacing:1px; margin-bottom:4px;">
                    Lead PNR Identifier
                  </div>
                  <div style="font-size:24px; font-weight:900; color:#0F3D56; letter-spacing:2px; font-family:Courier, monospace;">
                    {pb.ref_id}
                  </div>
                  <div style="font-size:11px; color:#64748b; margin-top:4px;">
                    Submitted: <strong>{submitted_ist}</strong>
                  </div>
                </td>
              </tr>
            </table>

            <!-- LEAD DETAILS TABLE -->
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="border:1px solid #E2E8F0; border-radius:12px; overflow:hidden; margin-bottom:20px; border-collapse:collapse;">
              <tr style="background-color:#F8FAFC;">
                <td colspan="2" style="padding:12px 18px; border-bottom:1px solid #E2E8F0; font-size:11px; font-weight:800; color:#475569; letter-spacing:1px; text-transform:uppercase;">
                  👤 Customer & Booking Details
                </td>
              </tr>
              <tr>
                <td style="padding:11px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6; width:38%;">PNR Number</td>
                <td style="padding:11px 18px; font-size:13px; color:#1598a1; font-weight:900; text-align:right; border-bottom:1px solid #eef2f6; font-family:Courier, monospace; letter-spacing:1px;">{pb.ref_id}</td>
              </tr>
              <tr>
                <td style="padding:11px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Customer Name</td>
                <td style="padding:11px 18px; font-size:13px; color:#0F3D56; font-weight:800; text-align:right; border-bottom:1px solid #eef2f6;">{pb.customer_name}</td>
              </tr>
              <tr>
                <td style="padding:11px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Customer Phone</td>
                <td style="padding:11px 18px; font-size:13px; color:#0F3D56; font-weight:800; text-align:right; border-bottom:1px solid #eef2f6;">
                  <a href="tel:{formatted_phone}" style="color:#1598a1; text-decoration:none;">{formatted_phone}</a>
                </td>
              </tr>
              <tr>
                <td style="padding:11px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Customer Email</td>
                <td style="padding:11px 18px; font-size:13px; color:#0F3D56; font-weight:700; text-align:right; border-bottom:1px solid #eef2f6;">
                  <a href="mailto:{pb.customer_email}" style="color:#1598a1; text-decoration:none;">{pb.customer_email}</a>
                </td>
              </tr>
              <tr>
                <td style="padding:11px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Package</td>
                <td style="padding:11px 18px; font-size:13px; color:#0F3D56; font-weight:700; text-align:right; border-bottom:1px solid #eef2f6;">{pb.package_name}</td>
              </tr>
              <tr>
                <td style="padding:11px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Requested Date</td>
                <td style="padding:11px 18px; font-size:13px; color:#0F3D56; font-weight:700; text-align:right; border-bottom:1px solid #eef2f6;">{travel}</td>
              </tr>
              <tr>
                <td style="padding:11px 18px; font-size:13px; color:#64748b; border-bottom:1px solid #eef2f6;">Travellers</td>
                <td style="padding:11px 18px; font-size:13px; color:#0F3D56; font-weight:700; text-align:right; border-bottom:1px solid #eef2f6;">{pax}</td>
              </tr>
              {notes_html}
              <tr>
                <td style="padding:11px 18px; font-size:13px; color:#64748b;">Lead Source</td>
                <td style="padding:11px 18px; font-size:12px; color:#475569; text-align:right;">Online Pre-Booking (<a href="https://tstelanganatourism.com/prebooking" style="color:#1598a1; text-decoration:none;">tstelanganatourism.com/prebooking</a>)</td>
              </tr>
            </table>

            <!-- ACTION REQUIRED (AMBER BOX) -->
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#FFFBEB; border:1px solid #FDE68A; border-radius:12px; margin-bottom:20px;">
              <tr>
                <td style="padding:16px 20px;">
                  <div style="font-size:12px; font-weight:800; color:#B45309; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px;">
                    ⚡ Action Required within 24 Hours:
                  </div>
                  <ul style="margin:0; padding-left:18px; font-size:12px; color:#78350F; line-height:20px;">
                    <li>Check cruise & stay capacity for <strong>{travel}</strong>.</li>
                    <li>Contact customer at <strong>{formatted_phone}</strong> to confirm booking.</li>
                    <li>Update lead status to Contacted / Confirmed in the Admin Dashboard.</li>
                  </ul>
                </td>
              </tr>
            </table>

            <!-- QUICK CONTACT BUTTONS -->
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="margin-bottom:12px;">
              <tr>
                <td style="padding-bottom:10px;">
                  <a href="{wa_url}" target="_blank" style="display:block; width:100%; box-sizing:border-box; background-color:#25D366; color:#ffffff; font-size:14px; font-weight:800; text-align:center; text-decoration:none; padding:14px 20px; border-radius:10px; letter-spacing:0.3px;">
                    💬 Contact Customer on WhatsApp ({formatted_phone})
                  </a>
                </td>
              </tr>
              <tr>
                <td style="padding-bottom:10px;">
                  <a href="tel:{formatted_phone}" style="display:block; width:100%; box-sizing:border-box; background-color:#0F3D56; color:#ffffff; font-size:14px; font-weight:800; text-align:center; text-decoration:none; padding:13px 20px; border-radius:10px;">
                    📞 Direct Call to Customer: {formatted_phone}
                  </a>
                </td>
              </tr>
              <tr>
                <td>
                  <a href="https://tstelanganatourism.com/admin/pre-bookings" target="_blank" style="display:block; width:100%; box-sizing:border-box; background-color:#F0FAF9; border:1.5px solid #1598a1; color:#0F3D56; font-size:13px; font-weight:800; text-align:center; text-decoration:none; padding:12px 20px; border-radius:10px;">
                    📋 Open Pre-Bookings Admin Dashboard →
                  </a>
                </td>
              </tr>
            </table>

          </td>
        </tr>

        <!-- ADMIN FOOTER -->
        <tr>
          <td align="center" style="background-color:#F8FAFC; border-top:1px solid #E2E8F0; padding:18px 24px;">
            <div style="font-size:11px; color:#94a3b8; line-height:18px;">
              TS Boat Tourism Internal Portal · Bhadrachalam Office (+91 99513 69573)<br>
              Official Tourism Management System · <a href="https://tstelanganatourism.com" target="_blank" style="color:#1598a1; text-decoration:none; font-weight:600;">tstelanganatourism.com</a>
            </div>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


# ── Daily Seat Capacity ────────────────────────────────────────────────────────
DAILY_SEAT_CAPACITY = 100

# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/availability")
async def get_prebooking_availability(
    package_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns daily available seats for the given package in September 2026.
    Base capacity: 100 seats per day.
    Count dynamically reduces on demand as bookings are made.
    """
    from sqlalchemy import select, func
    min_date = date(2026, 9, 1)
    max_date = date(2026, 9, 30)

    stmt = (
        select(
            PreBooking.travel_date,
            func.coalesce(func.sum(PreBooking.adult_count + PreBooking.child_count), 0).label("booked_seats"),
        )
        .where(
            PreBooking.package_id == package_id,
            PreBooking.travel_date >= min_date,
            PreBooking.travel_date <= max_date,
        )
        .group_by(PreBooking.travel_date)
    )
    result = await db.execute(stmt)
    booked_map = {row.travel_date.isoformat(): int(row.booked_seats or 0) for row in result.all()}

    availability = {}
    for day in range(1, 31):
        d_str = f"2026-09-{day:02d}"
        booked = booked_map.get(d_str, 0)
        availability[d_str] = max(0, DAILY_SEAT_CAPACITY - booked)

    return {
        "package_id": package_id,
        "daily_capacity": DAILY_SEAT_CAPACITY,
        "availability": availability,
    }


@router.post("", response_model=PreBookingResponse, status_code=status.HTTP_201_CREATED)
async def create_pre_booking(
    payload: PreBookingCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Submit an early pre-booking lead from the /prebooking page."""
    # Rate limit
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    # Validate date (September 2026 only)
    min_date = date(2026, 9, 1)
    max_date = date(2026, 9, 30)
    if payload.travel_date < min_date or payload.travel_date > max_date:
        raise HTTPException(
            status_code=400,
            detail="Travel date must be within September 2026."
        )

    # Check available seats (100 per day capacity)
    from sqlalchemy import select, func
    total_requested = payload.adult_count + payload.child_count
    booked_stmt = select(
        func.coalesce(func.sum(PreBooking.adult_count + PreBooking.child_count), 0)
    ).where(
        PreBooking.package_id == payload.package_id,
        PreBooking.travel_date == payload.travel_date,
    )
    booked_result = await db.execute(booked_stmt)
    already_booked = booked_result.scalar_one() or 0
    remaining_seats = max(0, DAILY_SEAT_CAPACITY - already_booked)

    if total_requested > remaining_seats:
        raise HTTPException(
            status_code=400,
            detail=f"Only {remaining_seats} seat{'s' if remaining_seats != 1 else ''} available for {payload.travel_date.strftime('%d %b %Y')}. You requested {total_requested}."
        )

    # Generate PNR number: package name first 3 letters + booked date (e.g. ABC23092026)
    import re
    from sqlalchemy import select
    clean_name = re.sub(r'[^A-Za-z]', '', payload.package_name)
    prefix = (clean_name[:3] if len(clean_name) >= 3 else clean_name.ljust(3, 'X')).upper()
    date_str = payload.travel_date.strftime("%d%m%Y")  # e.g. 23092026
    base_pnr = f"{prefix}{date_str}"

    # Ensure uniqueness in case of multiple bookings on the same date for the same package
    candidate_pnr = base_pnr
    seq = 1
    while True:
        existing = await db.execute(select(PreBooking).where(PreBooking.ref_id == candidate_pnr))
        if not existing.scalar_one_or_none():
            break
        seq += 1
        candidate_pnr = f"{base_pnr}-{seq}"

    pnr_number = candidate_pnr

    pb = PreBooking(
        ref_id=pnr_number,
        package_id=payload.package_id,
        package_name=payload.package_name,
        travel_date=payload.travel_date,
        adult_count=payload.adult_count,
        child_count=payload.child_count,
        customer_name=payload.customer_name,
        customer_email=str(payload.customer_email),
        customer_phone=payload.customer_phone,
        notes=payload.notes,
    )
    db.add(pb)
    await db.commit()
    await db.refresh(pb)

    # Dispatch emails asynchronously in background — instant response (<30ms) for user!
    background_tasks.add_task(_dispatch_prebooking_emails, pb.id)

    # Build WhatsApp URL for success screen
    travel_str = pb.travel_date.strftime("%d %B %Y")
    pax = f"{pb.adult_count} adult{'s' if pb.adult_count != 1 else ''}"
    if pb.child_count:
        pax += f" + {pb.child_count} child{'ren' if pb.child_count > 1 else ''}"
    import urllib.parse
    wa_msg = (
        f"Hello TS Boat Tourism! I have pre-booked the *{pb.package_name}* "
        f"for *{travel_str}* ({pax}). My PNR Number is *{pnr_number}*. Please confirm my slot."
    )
    wa_url = f"https://wa.me/919951369573?text={urllib.parse.quote(wa_msg)}"

    return PreBookingResponse(
        pnr_number=pnr_number,
        ref_id=pnr_number,
        message="Pre-booking confirmed! Check your email and WhatsApp for details.",
        whatsapp_url=wa_url,
    )
