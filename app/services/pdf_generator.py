import logging
import asyncio
import sys
from html import escape
from typing import Optional
from datetime import datetime, timezone
from playwright.async_api import async_playwright

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.core.config import settings
from app.services.r2_storage import r2_service
from app.db.session import AsyncSessionLocal
from app.models.package import Package
from app.models.room import Room
from app.models.enums import DocumentGenerationStatus
from app.utils.cache import clear_cache_prefix

logger = logging.getLogger(__name__)

AP_TOURISM_EMAIL_LOGO_URL = "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1779358705/b66b077a-69fa-4625-8b49-9a168efde88f.png"
TS_TOURISM_EMAIL_LOGO_URL = "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1779358643/22175967-f7df-420e-adcd-b4a37725fd5f.png"

async def generate_pdf_from_url(url: str) -> bytes:
    """
    Spins up headless Chromium, navigates to the URL, and generates a PDF.
    Used ONLY for package brochures (admin-triggered, one-time operations).
    Booking tickets/invoices/forms use client-side printing instead.

    CRITICAL: browser.close() is always called in the finally block to prevent
    Chromium zombie processes from leaking if navigation fails or times out.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        try:
            page = await browser.new_page()
            logger.info(f"Navigating to {url} for brochure PDF generation")
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            if not response or not response.ok:
                status = response.status if response else "no response"
                raise Exception(f"Failed to load PDF page {url}: HTTP {status}")

            await page.wait_for_selector(".page", timeout=15000)
            await page.emulate_media(media="print")

            try:
                await page.evaluate("""
                    async () => {
                        const images = Array.from(document.images);
                        await Promise.all(images.map(img => {
                            if (img.complete) return Promise.resolve();
                            return new Promise((resolve) => {
                                img.addEventListener('load', resolve);
                                img.addEventListener('error', resolve);
                            });
                        }));
                    }
                """)
                await page.wait_for_timeout(500)
            except Exception as e:
                logger.warning(f"Timeout or error waiting for images on {url}: {e}")

            pdf_bytes = await page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"}
            )
            return pdf_bytes
        finally:
            await browser.close()


def sync_generate_pdf(url: str) -> bytes:
    """
    Synchronous wrapper to run generate_pdf_from_url in a dedicated thread.
    Used ONLY for package brochure generation.
    """
    if sys.platform == 'win32':
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(generate_pdf_from_url(url))
    finally:
        loop.close()



async def generate_package_brochure_task(ctx, package_id: int):
    """
    Background task to generate a package brochure and upload to R2.
    """
    async with AsyncSessionLocal() as db:
        package = await db.get(Package, package_id)
        if not package:
            logger.error(f"Package {package_id} not found for brochure generation.")
            return
            
        try:
            # 1. Update status to GENERATING
            package.brochure_generation_status = DocumentGenerationStatus.GENERATING
            await db.commit()
            
            # 2. Generate PDF using a separate thread with its own ProactorEventLoop on Windows
            frontend_url = settings.FRONTEND_URL.rstrip('/')
            print_url = f"{frontend_url}/print/package/{package.slug}"
            pdf_bytes = await asyncio.to_thread(sync_generate_pdf, print_url)
            
            # 3. Upload to R2
            version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            object_name = f"private/brochures/generated/package_{package.slug}_{version}.pdf"
            await r2_service.upload_file(pdf_bytes, object_name, content_type="application/pdf")
            
            # 4. Clean up old generated brochure if exists and different
            if package.generated_brochure_url and package.generated_brochure_url != object_name:
                await r2_service.delete_file(package.generated_brochure_url)
                
            # 5. Update DB
            package.generated_brochure_url = object_name
            package.brochure_generation_status = DocumentGenerationStatus.AVAILABLE
            await db.commit()
            clear_cache_prefix("packages:list:")
            clear_cache_prefix(f"packages:detail:{package.slug}")
            logger.info(f"Successfully generated and uploaded brochure for package {package.slug}")
            
        except Exception as e:
            logger.exception(f"Failed to generate brochure for package {package_id}: {e}")
            package.brochure_generation_status = DocumentGenerationStatus.FAILED
            await db.commit()
            raise e # Raise to let ARQ handle retries if applicable

async def process_post_booking_documents_task(ctx, booking_id: int, is_fully_paid: bool = None):
    """
    Background task to send booking confirmation email.
    PDF generation is client-side — the email contains links to the beautiful
    frontend print pages (/print/ticket, /print/invoice, /print/form) which
    open the customer's browser print dialog directly. This eliminates
    all Playwright RAM usage while keeping the full ticket design.
    """
    from app.models.booking import Booking, EmailLog
    from app.models.enums import BookingStatus, DocumentGenerationStatus
    from app.utils.pricing import get_booking_hash
    from app.models.user import User
    from app.services.email_service import email_service
    from app.services.admin_notification import send_admin_booking_notification

    async with AsyncSessionLocal() as db:
        stmt = select(Booking).options(selectinload(Booking.passengers)).where(Booking.id == booking_id)
        result = await db.execute(stmt)
        booking = result.scalars().first()
        if not booking:
            logger.error(f"Booking {booking_id} not found for email dispatch.")
            return

        # Build the signed print URLs — these point to the beautiful React ticket pages.
        # The customer clicks the link, their browser loads the full ticket design,
        # and they press the built-in "Print / Save PDF" button to download it.
        signature = get_booking_hash(booking.public_id, settings.SECRET_KEY)
        frontend_url = settings.FRONTEND_URL.rstrip('/')
        ticket_url = f"{frontend_url}/print/ticket/{booking.public_id}?secret={signature}"

        # Resolve payment state
        if is_fully_paid is None:
            is_fully_paid = booking.remaining_balance <= 0 or booking.status == BookingStatus.FULLY_PAID

        # Mark ticket/invoice statuses as AVAILABLE immediately — the print pages are
        # always live because they are server-rendered React pages, not R2 files.
        try:
            booking.ticket_generation_status = DocumentGenerationStatus.AVAILABLE
            if is_fully_paid:
                booking.invoice_generation_status = DocumentGenerationStatus.AVAILABLE
            await db.commit()
            logger.info(f"Marked document statuses as AVAILABLE for booking {booking.public_id}")
        except Exception as e:
            logger.error(f"Failed to mark document statuses for booking {booking_id}: {e}")

        # Build invoice URL (only relevant if fully paid)
        invoice_url = f"{frontend_url}/print/invoice/{booking.public_id}?secret={signature}" if is_fully_paid else ""

        # Build form URL (only for package bookings, not room bookings)
        is_room_booking = booking.room_variant_id is not None
        form_url = f"{frontend_url}/print/form/{booking.public_id}?secret={signature}" if not is_room_booking else ""


        # 4. Determine Recipient Email
        recipient_email = None
        recipient_name = "Guest"
        if booking.user_id:
            user = await db.get(User, booking.user_id)
            if user and user.email:
                recipient_email = user.email
                recipient_name = user.full_name

        if not recipient_email and booking.agent_id:
            agent = await db.get(User, booking.agent_id)
            if agent and agent.email:
                recipient_email = agent.email
                recipient_name = agent.full_name

        # 5. Prepare and Send Email
        if not recipient_email:
            # Skip email logic
            log_entry = EmailLog(
                booking_id=booking.id,
                recipient_email=None,
                email_type="FULL_PAYMENT" if is_fully_paid else "PARTIAL_PAYMENT",
                delivery_status="SKIPPED",
                failure_reason="EMAIL_SKIPPED_NO_RECIPIENT"
            )
            db.add(log_entry)
            await db.commit()
            return

        # Build premium, email-client-safe HTML content.
        office_phone = "+91 95420 69573"
        office_address = "Telangana Boat Tourism Central Booking Office, D.No. 4-1-78/1, Kalyana Mandapam Road, Opp SBI ATM, Bhadrachalam, Bhadradri Kothagudem (Dist), Telangana - 507111."
        office_maps_url = "https://maps.app.goo.gl/6YDfViEq3RLuvNN36?g_st=awb"
        safe_recipient_name = escape(recipient_name or "Guest")
        safe_booking_id = escape(booking.public_id)
        safe_ticket_url = escape(ticket_url, quote=True)
        safe_form_url = escape(form_url, quote=True)
        safe_logo1_url = escape(AP_TOURISM_EMAIL_LOGO_URL, quote=True)
        safe_logo2_url = escape(TS_TOURISM_EMAIL_LOGO_URL, quote=True)
        safe_office_address = escape(office_address)
        safe_office_maps_url = escape(office_maps_url, quote=True)

        if is_room_booking:
            preview_text = "Your TS Tours booking documents are ready. Download your ticket before the link expires."
            next_steps_section = """
                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="margin:28px 0 20px 0;">
                                        <tr>
                                            <td style="padding:0;">
                                                <div style="font-family:Arial, Helvetica, sans-serif; color:#102f3a; font-size:18px; line-height:24px; font-weight:800; margin-bottom:8px;">Next steps</div>
                                                <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
                                                    <tr>
                                                        <td valign="top" width="26" style="padding:4px 0 0 0; color:#078a81; font-size:14px; font-family:Arial, Helvetica, sans-serif; font-weight:800;">1.</td>
                                                        <td style="padding:4px 0; color:#415865; font-size:14px; line-height:21px; font-family:Arial, Helvetica, sans-serif;">Download and print your ticket.</td>
                                                    </tr>
                                                    <tr>
                                                        <td valign="top" width="26" style="padding:4px 0 0 0; color:#078a81; font-size:14px; font-family:Arial, Helvetica, sans-serif; font-weight:800;">2.</td>
                                                        <td style="padding:4px 0; color:#415865; font-size:14px; line-height:21px; font-family:Arial, Helvetica, sans-serif;">Carry printed ticket during your journey.</td>
                                                    </tr>
                                                </table>
                                            </td>
                                        </tr>
                                    </table>
            """.strip()
            action_buttons_section = f"""
                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
                                        <tr>
                                            <td class="stack-column" width="100%" style="padding:0 0 12px 0;">
                                                <a href="{safe_ticket_url}" target="_blank" style="display:block; background-color:#078a81; border:1px solid #078a81; border-radius:10px; color:#ffffff; font-family:Arial, Helvetica, sans-serif; font-size:15px; line-height:20px; font-weight:800; padding:14px 16px; text-align:center; text-decoration:none;">Download Ticket</a>
                                            </td>
                                        </tr>
                                    </table>
            """.strip()
            mandatory_notice_section = """
                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="margin:4px 0 22px 0;">
                                        <tr>
                                            <td style="background-color:#fff7df; border:1px solid #f0d998; border-radius:12px; padding:14px 16px; color:#8a4b00; font-family:Arial, Helvetica, sans-serif; font-size:13px; line-height:20px; font-weight:800; text-align:center;">
                                                Mandatory: bring printed tickets for verification before boarding.
                                            </td>
                                        </tr>
                                    </table>
            """.strip()
        else:
            preview_text = "Your TS Tours booking documents are ready. Download your ticket and passenger form before the links expire."
            next_steps_section = """
                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="margin:28px 0 20px 0;">
                                        <tr>
                                            <td style="padding:0;">
                                                <div style="font-family:Arial, Helvetica, sans-serif; color:#102f3a; font-size:18px; line-height:24px; font-weight:800; margin-bottom:8px;">Next steps</div>
                                                <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
                                                    <tr>
                                                        <td valign="top" width="26" style="padding:4px 0 0 0; color:#078a81; font-size:14px; font-family:Arial, Helvetica, sans-serif; font-weight:800;">1.</td>
                                                        <td style="padding:4px 0; color:#415865; font-size:14px; line-height:21px; font-family:Arial, Helvetica, sans-serif;">Download and print your ticket.</td>
                                                    </tr>
                                                    <tr>
                                                        <td valign="top" width="26" style="padding:4px 0 0 0; color:#078a81; font-size:14px; font-family:Arial, Helvetica, sans-serif; font-weight:800;">2.</td>
                                                        <td style="padding:4px 0; color:#415865; font-size:14px; line-height:21px; font-family:Arial, Helvetica, sans-serif;">Download, fill, and print the passenger form.</td>
                                                    </tr>
                                                    <tr>
                                                        <td valign="top" width="26" style="padding:4px 0 0 0; color:#078a81; font-size:14px; font-family:Arial, Helvetica, sans-serif; font-weight:800;">3.</td>
                                                        <td style="padding:4px 0; color:#415865; font-size:14px; line-height:21px; font-family:Arial, Helvetica, sans-serif;">Carry both printed documents during your journey.</td>
                                                    </tr>
                                                </table>
                                            </td>
                                        </tr>
                                    </table>
            """.strip()
            action_buttons_section = f"""
                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0">
                                        <tr>
                                            <td class="stack-column" width="50%" style="padding:0 8px 12px 0;">
                                                <a href="{safe_ticket_url}" target="_blank" style="display:block; background-color:#078a81; border:1px solid #078a81; border-radius:10px; color:#ffffff; font-family:Arial, Helvetica, sans-serif; font-size:15px; line-height:20px; font-weight:800; padding:14px 16px; text-align:center; text-decoration:none;">Download Ticket</a>
                                            </td>
                                            <td class="stack-column" width="50%" style="padding:0 0 12px 8px;">
                                                <a href="{safe_form_url}" target="_blank" style="display:block; background-color:#ffffff; border:1px solid #075b60; border-radius:10px; color:#075b60; font-family:Arial, Helvetica, sans-serif; font-size:15px; line-height:20px; font-weight:800; padding:14px 16px; text-align:center; text-decoration:none;">Download Form</a>
                                            </td>
                                        </tr>
                                    </table>
            """.strip()
            mandatory_notice_section = """
                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="margin:4px 0 22px 0;">
                                        <tr>
                                            <td style="background-color:#fff7df; border:1px solid #f0d998; border-radius:12px; padding:14px 16px; color:#8a4b00; font-family:Arial, Helvetica, sans-serif; font-size:13px; line-height:20px; font-weight:800; text-align:center;">
                                                Mandatory: bring printed forms and tickets for verification before boarding.
                                            </td>
                                        </tr>
                                    </table>
            """.strip()

        base_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <meta name="x-apple-disable-message-reformatting">
            <title>{subject}</title>
            <style>
                body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
                table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; }}
                img {{ -ms-interpolation-mode: bicubic; border: 0; outline: none; text-decoration: none; }}
                @media only screen and (max-width: 620px) {{
                    .email-shell {{ width: 100% !important; }}
                    .mobile-pad {{ padding-left: 20px !important; padding-right: 20px !important; }}
                    .stack-column {{ display: block !important; width: 100% !important; max-width: 100% !important; }}
                    .stack-spacer {{ height: 12px !important; line-height: 12px !important; }}
                    .mobile-center {{ text-align: center !important; }}
                    .logo-img {{ width: 74px !important; height: 74px !important; }}
                    .brand-title {{ font-size: 24px !important; line-height: 30px !important; }}
                }}
            </style>
        </head>
        <body style="margin:0; padding:0; background-color:#eef3f6;">
            <div style="display:none; max-height:0; overflow:hidden; opacity:0; color:transparent;">
                {preview_text}
            </div>
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#eef3f6; margin:0; padding:0;">
                <tr>
                    <td align="center" style="padding:28px 12px;">
                        <table role="presentation" class="email-shell" width="640" border="0" cellpadding="0" cellspacing="0" style="width:640px; max-width:640px; background-color:#ffffff; border:1px solid #dbe6ea; border-radius:18px; overflow:hidden;">
                            <tr>
                                <td align="center" class="mobile-pad" style="background-color:#075b60; padding:30px 34px 26px 34px;">
                                    <table role="presentation" width="190" border="0" cellpadding="0" cellspacing="0" style="width:190px; margin:0 auto 16px auto;">
                                        <tr>
                                            <td align="center" width="95" style="padding:0 7px;">
                                                <img class="logo-img" src="{logo1_url}" width="82" height="82" alt="APTDC" style="display:block; width:82px; height:82px; border-radius:41px;">
                                            </td>
                                            <td align="center" width="95" style="padding:0 7px;">
                                                <img class="logo-img" src="{logo2_url}" width="82" height="82" alt="Telangana Tourism" style="display:block; width:82px; height:82px; border-radius:41px;">
                                            </td>
                                        </tr>
                                    </table>
                                    <div class="brand-title" style="font-family:Arial, Helvetica, sans-serif; color:#ffffff; font-size:28px; line-height:34px; font-weight:800; letter-spacing:0; margin:4px 0 6px 0;">TS Boating &amp; Tourism</div>
                                    <div style="font-family:Arial, Helvetica, sans-serif; color:#d6f4ef; font-size:14px; line-height:20px; font-weight:700;">Booking documents ready</div>
                                </td>
                            </tr>
                            <tr>
                                <td class="mobile-pad" style="padding:36px 42px 24px 42px; font-family:Arial, Helvetica, sans-serif; color:#14313a;">
                                    <p style="margin:0 0 16px 0; color:#102f3a; font-size:22px; line-height:29px; font-weight:800;">Hello {recipient_name},</p>
                                    <p style="margin:0 0 24px 0; color:#415865; font-size:15px; line-height:24px;">{message_body}</p>

                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="border:1px solid #dbe6ea; border-radius:14px; background-color:#f8fbfc;">
                                        <tr>
                                            <td style="padding:18px 20px; border-bottom:1px solid #dbe6ea;">
                                                <div style="font-family:Arial, Helvetica, sans-serif; color:#607380; font-size:11px; line-height:16px; font-weight:800; text-transform:uppercase;">Booking reference</div>
                                                <div style="font-family:Arial, Helvetica, sans-serif; color:#075b60; font-size:20px; line-height:28px; font-weight:800;">{booking_id}</div>
                                            </td>
                                        </tr>
                                        {financial_details}
                                    </table>

                                    {next_steps_section}

                                    {action_buttons_section}

                                    {mandatory_notice_section}
                                </td>
                            </tr>
                            <tr>
                                <td align="center" class="mobile-pad" style="background-color:#f7fafb; border-top:1px solid #dbe6ea; padding:24px 34px 28px 34px; font-family:Arial, Helvetica, sans-serif;">
                                    <div style="color:#102f3a; font-size:15px; line-height:22px; font-weight:800; margin-bottom:7px;">Thank you for choosing TS Boating &amp; Tourism.</div>
                                    <div style="color:#526a76; font-size:13px; line-height:20px; margin-bottom:14px;">For booking support, call <a href="tel:{office_phone_tel}" style="color:#075b60; font-weight:800; text-decoration:none;">{office_phone}</a></div>
                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border:1px solid #dbe6ea; border-radius:12px;">
                                        <tr>
                                            <td align="left" style="padding:15px 16px; font-family:Arial, Helvetica, sans-serif;">
                                                <div style="color:#102f3a; font-size:13px; line-height:19px; font-weight:800; margin-bottom:5px;">Manual ticket collection office</div>
                                                <div style="color:#526a76; font-size:12px; line-height:18px; margin-bottom:12px;">{office_address}</div>
                                                <a href="{office_maps_url}" target="_blank" style="display:inline-block; background-color:#075b60; border-radius:8px; color:#ffffff; font-family:Arial, Helvetica, sans-serif; font-size:12px; line-height:18px; font-weight:800; padding:9px 13px; text-decoration:none;">Open Google Maps</a>
                                            </td>
                                        </tr>
                                    </table>
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """

        amount_paid_row = f"""
                        <tr>
                            <td style="padding:16px 20px; border-bottom:1px solid #dbe6ea;">
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#607380; font-size:11px; line-height:16px; font-weight:800; text-transform:uppercase;">Amount paid</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#102f3a; font-size:17px; line-height:25px; font-weight:800;">Rs. {float(booking.paid_amount):.2f}</div>
                            </td>
                        </tr>
        """

        if is_fully_paid:
            email_type = "FULL_PAYMENT"
            subject = "Booking Confirmed - TS Tours"
            if is_room_booking:
                message_body = f"Your booking <strong style=\"color:#075b60;\">{safe_booking_id}</strong> is confirmed and fully paid. Your official ticket is ready below."
            else:
                message_body = f"Your booking <strong style=\"color:#075b60;\">{safe_booking_id}</strong> is confirmed and fully paid. Your official ticket and passenger form are ready below."
            financial_details = f"""
                        <tr>
                            <td style="padding:16px 20px; border-bottom:1px solid #dbe6ea;">
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#607380; font-size:11px; line-height:16px; font-weight:800; text-transform:uppercase;">Payment status</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#05845f; font-size:17px; line-height:25px; font-weight:800;">Fully paid</div>
                            </td>
                        </tr>
                        {amount_paid_row}
            """.rstrip()
        else:
            email_type = "PARTIAL_PAYMENT"
            subject = "Booking Payment Received - TS Tours"
            if is_room_booking:
                message_body = f"We received your payment for booking <strong style=\"color:#075b60;\">{safe_booking_id}</strong>. Please clear the remaining balance before your journey and keep the ticket below ready."
            else:
                message_body = f"We received your payment for booking <strong style=\"color:#075b60;\">{safe_booking_id}</strong>. Please clear the remaining balance before your journey and keep the documents below ready."
            financial_details = f"""
                        {amount_paid_row}
                        <tr>
                            <td style="padding:16px 20px;">
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#607380; font-size:11px; line-height:16px; font-weight:800; text-transform:uppercase;">Balance remaining</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#c2410c; font-size:17px; line-height:25px; font-weight:800;">Rs. {float(booking.remaining_balance):.2f}</div>
                            </td>
                        </tr>
            """.rstrip()

        html_content = base_html.format(
            subject=escape(subject),
            recipient_name=safe_recipient_name,
            message_body=message_body,
            booking_id=safe_booking_id,
            financial_details=financial_details,
            logo1_url=safe_logo1_url,
            logo2_url=safe_logo2_url,
            office_phone=office_phone,
            office_phone_tel=office_phone.replace(" ", ""),
            office_address=safe_office_address,
            office_maps_url=safe_office_maps_url,
            preview_text=preview_text,
            next_steps_section=next_steps_section,
            action_buttons_section=action_buttons_section,
            mandatory_notice_section=mandatory_notice_section
        )


        success, error_reason = await email_service.send_booking_email(
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            subject=subject,
            html_content=html_content
        )
        
        # Send admin notification
        try:
            await send_admin_booking_notification(booking)
        except Exception as e:
            logger.error(f"Failed to dispatch admin notification: {e}")

        log_entry = EmailLog(
            booking_id=booking.id,
            recipient_email=recipient_email,
            email_type=email_type,
            delivery_status="SENT" if success else "FAILED",
            failure_reason=error_reason if not success else None
        )
        db.add(log_entry)
        await db.commit()
