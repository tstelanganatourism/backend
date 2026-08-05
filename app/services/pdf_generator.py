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
from app.db.session import AsyncSessionLocal
import cloudinary
import cloudinary.uploader

# Cloudinary configuration is initialized globally when app loads, but we ensure it is ready
if settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True
    )

def sync_cloudinary_upload(pdf_bytes: bytes, filename: str) -> str:
    res = cloudinary.uploader.upload(
        pdf_bytes,
        folder="ts_tours/brochures",
        resource_type="raw",
        public_id=filename
    )
    return res.get("secure_url")

def sync_cloudinary_delete(url: str):
    if not url:
        return
    try:
        parts = url.split("/upload/")
        if len(parts) > 1:
            path_parts = parts[1].split("/")
            if path_parts[0].startswith("v") and path_parts[0][1:].isdigit():
                public_id = "/".join(path_parts[1:])
            else:
                public_id = "/".join(path_parts)
            # For raw file resource types, the extension is part of public_id
            cloudinary.uploader.destroy(public_id, resource_type="raw")
    except Exception as e:
        logger.error(f"Failed to delete Cloudinary PDF: {url}. Error: {e}")
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
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()
            logger.info(f"Navigating to {url} for brochure PDF generation")
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            if not response or not response.ok:
                status = response.status if response else "no response"
                raise Exception(f"Failed to load PDF page {url}: HTTP {status}")

            await page.wait_for_selector(".brochure-container", timeout=15000)
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
            
            if settings.ENVIRONMENT == "development":
                frontend_url = "http://localhost:3000"
            else:
                frontend_url = settings.FRONTEND_URL.rstrip('/')
            print_url = f"{frontend_url}/print/package/{package.slug}"
            pdf_bytes = await asyncio.to_thread(sync_generate_pdf, print_url)
            
            # 3. Upload to Cloudinary
            version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            filename = f"package_{package.slug}_{version}"
            cloudinary_url = await asyncio.to_thread(sync_cloudinary_upload, pdf_bytes, filename)
            
            # 4. Clean up old generated brochure if exists and different
            if package.generated_brochure_url and package.generated_brochure_url != cloudinary_url:
                await asyncio.to_thread(sync_cloudinary_delete, package.generated_brochure_url)
                
            # 5. Update DB
            package.generated_brochure_url = cloudinary_url
            package.brochure_generation_status = DocumentGenerationStatus.AVAILABLE
            await db.commit()
            clear_cache_prefix("packages:list:")
            clear_cache_prefix(f"packages:detail:{package.slug}")
            logger.info(f"Successfully generated and uploaded brochure for package {package.slug}")
            
        except (Exception, asyncio.CancelledError) as e:
            logger.exception(f"Failed to generate brochure for package {package_id}: {e}")
            
            async def set_failed():
                try:
                    async with AsyncSessionLocal() as fail_db:
                        fail_package = await fail_db.get(Package, package_id)
                        if fail_package:
                            fail_package.brochure_generation_status = DocumentGenerationStatus.FAILED
                            await fail_db.commit()
                            logger.info(f"Successfully marked package {package_id} brochure status as FAILED")
                except Exception as db_err:
                    logger.error(f"Failed to set brochure status to FAILED in cleanup for package {package_id}: {db_err}")

            # Run the database update in a separate task and shield it
            # This ensures it runs to completion even if the current task is cancelled
            cleanup_task = asyncio.create_task(set_failed())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # Still raise the original CancelledError
                pass
            raise e

async def process_post_booking_documents_task(ctx, booking_id: int, is_fully_paid: bool = None, is_postponement: bool = False):
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


        # 4. Determine Recipient Emails
        recipients = []
        
        # Tourist Email (Highest Priority)
        if booking.customer_email:
            primary_passenger_name = next((p.full_name for p in booking.passengers if p.is_primary), "Guest")
            recipients.append((booking.customer_email, primary_passenger_name))
            
        # Logged-in User Email (If no tourist email provided)
        elif booking.user_id:
            user = await db.get(User, booking.user_id)
            if user and user.email:
                recipients.append((user.email, user.full_name))

        # Agent Email (If booked via agent, always send them a copy)
        if booking.agent_id:
            agent = await db.get(User, booking.agent_id)
            if agent and agent.email:
                if not any(r[0] == agent.email for r in recipients):
                    recipients.append((agent.email, agent.full_name))

        # 5. Prepare and Send Email
        has_failure = False

        if not recipients:
            if not is_postponement:
                # Send admin notification even if customer has no email (walk-in/guest/admin direct)
                try:
                    from app.services.admin_notification import send_admin_booking_notification
                    admin_success = await send_admin_booking_notification(booking, db=db)
                    if not admin_success:
                        has_failure = True
                except Exception as e:
                    logger.error(f"Failed to dispatch admin notification for recipient-less booking: {e}")
                    has_failure = True

            # Skip customer email but log the skip
            log_entry = EmailLog(
                booking_id=booking.id,
                recipient_email=None,
                email_type="FULL_PAYMENT" if is_fully_paid else "PARTIAL_PAYMENT",
                delivery_status="SKIPPED",
                failure_reason="EMAIL_SKIPPED_NO_RECIPIENT"
            )
            db.add(log_entry)
            await db.commit()
            logger.info(f"No customer email recipient for booking {booking_id}; admin notified.")
            if has_failure:
                raise Exception(f"Admin email failed for recipient-less booking {booking_id}. ARQ will retry.")
            return

        # Build premium, email-client-safe HTML content.
        office_phone = "+91 99513 69573, +91 77801 19268"
        office_address = "DOOR NO: 10-1-2/1, GROUND FLOOR, OM SHANTHI BUILDING SATARAM, BHADRACHALAM, BHADRADRI KOTHAGUDEM (DIST), TELANGANA-507111"
        office_maps_url = "https://maps.app.goo.gl/b9ZvxUvvFq6FgKVU8"
        # Recipient name is handled per-recipient in _generate_html
        safe_booking_id = escape(booking.public_id)
        safe_ticket_url = escape(ticket_url, quote=True)
        safe_form_url = escape(form_url, quote=True)
        safe_logo1_url = escape(AP_TOURISM_EMAIL_LOGO_URL, quote=True)
        safe_logo2_url = escape(TS_TOURISM_EMAIL_LOGO_URL, quote=True)
        safe_office_address = escape(office_address)
        safe_office_maps_url = escape(office_maps_url, quote=True)

        target_name = "—"
        try:
            if is_room_booking:
                from app.models.room import RoomVariant, Room, RoomSlotInventory
                slot_start = None
                slot_end = None
                if booking.pricing_snapshot:
                    slot_start = booking.pricing_snapshot.get("slot_start")
                    slot_end = booking.pricing_snapshot.get("slot_end")
                
                inv_stmt = select(RoomSlotInventory).where(
                    RoomSlotInventory.room_variant_id == booking.room_variant_id,
                    RoomSlotInventory.date == booking.travel_date
                )
                if slot_start and slot_end:
                    from datetime import datetime
                    try:
                        s_time = datetime.strptime(slot_start, "%H:%M:%S").time()
                        e_time = datetime.strptime(slot_end, "%H:%M:%S").time()
                        inv_stmt = inv_stmt.where(
                            RoomSlotInventory.slot_start == s_time,
                            RoomSlotInventory.slot_end == e_time
                        )
                    except Exception:
                        pass
                inv_res = await db.execute(inv_stmt)
                inv_row = inv_res.scalars().first()
                
                res = await db.execute(select(Room.lodge_name, RoomVariant.variant_name).join(RoomVariant).where(RoomVariant.id == booking.room_variant_id))
                room_data = res.first()
                if inv_row and inv_row.hotel_name:
                    target_name = f"{inv_row.hotel_name} — {room_data[0]} ({room_data[1]})" if room_data else inv_row.hotel_name
                else:
                    if room_data:
                        target_name = f"{room_data[0]} ({room_data[1]})"
            else:
                from app.models.package import PackageVariant, Package
                res = await db.execute(select(Package.title).join(PackageVariant).where(PackageVariant.id == booking.variant_id))
                pkg_data = res.first()
                if pkg_data:
                    target_name = pkg_data[0]
        except Exception as e:
            logger.warning(f"Could not fetch target name for booking {booking.public_id}: {e}")
        safe_target_name = escape(target_name)
        safe_target_label = "Booked Room" if is_room_booking else "Booked Package"

        if is_room_booking:
            preview_text = "Your TS Boat Tourism booking documents are ready. Download your ticket before the link expires."
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
            preview_text = "Your TS Boat Tourism booking documents are ready. Download your ticket and passenger form before the links expire."
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
                body {{ margin: 0; padding: 0; background-color: #f3f4f6; }}
                @media only screen and (max-width: 620px) {{
                    .email-shell {{ width: 100% !important; }}
                    .mobile-pad {{ padding-left: 20px !important; padding-right: 20px !important; }}
                    .stack-column {{ display: block !important; width: 100% !important; max-width: 100% !important; }}
                    .stack-spacer {{ height: 12px !important; line-height: 12px !important; }}
                    .mobile-center {{ text-align: center !important; }}
                    .logo-img {{ width: 64px !important; height: 64px !important; }}
                    .brand-title {{ font-size: 22px !important; line-height: 28px !important; }}
                }}
            </style>
        </head>
        <body style="margin:0; padding:0; background-color:#f3f4f6; font-family:'Helvetica Neue', Helvetica, Arial, sans-serif;">
            <div style="display:none; max-height:0; overflow:hidden; opacity:0; color:transparent;">
                {preview_text}
            </div>
            <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#f3f4f6; margin:0; padding:0;">
                <tr>
                    <td align="center" style="padding:40px 12px;">
                        <table role="presentation" class="email-shell" width="600" border="0" cellpadding="0" cellspacing="0" style="width:600px; max-width:600px; background-color:#ffffff; border:1px solid #e2e8f0; border-radius:24px; overflow:hidden; box-shadow: 0 10px 30px rgba(10, 35, 81, 0.05);">
                            <!-- Header -->
                            <tr>
                                <td align="center" class="mobile-pad" style="background-color:#0a2351; padding:36px 40px 36px 40px; border-bottom: 4px solid #c8a45a;">
                                    <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin:0 auto 12px auto;">
                                        <tr>
                                            <td align="center">
                                                <img class="logo-img" src="https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1779358643/22175967-f7df-420e-adcd-b4a37725fd5f.png" width="76" height="76" alt="TS Boat Tourism" style="display:block; width:76px; height:76px; border:0; outline:none; text-decoration:none; margin:0 auto;">
                                            </td>
                                        </tr>
                                    </table>
                                    <div class="brand-title" style="color:#ffffff; font-size:26px; line-height:32px; font-weight:800; letter-spacing:-0.5px; margin:0 0 4px 0; font-family:'Outfit', Arial, sans-serif;">TS Boat Tourism</div>
                                    <div style="color:#c8a45a; font-size:12px; line-height:16px; font-weight:800; letter-spacing:1.5px; text-transform:uppercase;">Official Booking Platform</div>
                                </td>
                            </tr>
                            <!-- Body Content -->
                            <tr>
                                <td class="mobile-pad" style="padding:40px 48px 32px 48px; color:#1e293b;">
                                    <p style="margin:0 0 16px 0; color:#0a2351; font-size:22px; line-height:28px; font-weight:800; letter-spacing:-0.5px;">Hello {recipient_name},</p>
                                    <p style="margin:0 0 24px 0; color:#475569; font-size:15px; line-height:24px; font-weight:500;">{message_body}</p>

                                    <!-- Summary Table -->
                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="border:1px solid #e2e8f0; border-radius:16px; background-color:#f8fafc; margin-bottom:24px; overflow:hidden;">
                                        <tr>
                                            <td style="padding:16px 20px; border-bottom:1px solid #e2e8f0; background: #fafafb;">
                                                <div style="color:#64748b; font-size:10px; line-height:14px; font-weight:800; text-transform:uppercase; letter-spacing:1px; margin-bottom:2px;">Booking reference</div>
                                                <div style="color:#0a2351; font-size:18px; line-height:24px; font-weight:800; font-family:Courier, monospace;">{booking_id}</div>
                                            </td>
                                        </tr>
                                        <tr>
                                            <td style="padding:16px 20px; border-bottom:1px solid #e2e8f0;">
                                                <div style="color:#64748b; font-size:10px; line-height:14px; font-weight:800; text-transform:uppercase; letter-spacing:1px; margin-bottom:2px;">{safe_target_label}</div>
                                                <div style="color:#0a2351; font-size:15px; line-height:20px; font-weight:800;">{safe_target_name}</div>
                                            </td>
                                        </tr>
                                        {financial_details}
                                    </table>

                                    {next_steps_section}

                                    {action_buttons_section}

                                    {mandatory_notice_section}
                                </td>
                            </tr>
                            <!-- Footer Support & Map -->
                            <tr>
                                <td align="center" class="mobile-pad" style="background-color:#0a2351; border-top:2px solid #c8a45a; padding:32px 40px; color:#cbd5e1; font-size:12px; line-height:18px;">
                                    <div style="color:#ffffff; font-size:15px; line-height:22px; font-weight:800; margin-bottom:6px;">Thank you for choosing TS Boat Tourism.</div>
                                    <div style="margin-bottom:20px; font-weight:700;">For booking support, call <a href="tel:{office_phone_tel}" style="color:#c8a45a; font-weight:800; text-decoration:none;">{office_phone}</a></div>
                                    
                                    <table role="presentation" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-color:#1e293b; border:1px solid #334155; border-radius:16px;">
                                        <tr>
                                            <td align="left" style="padding:20px 20px;">
                                                <div style="color:#ffffff; font-size:13px; line-height:18px; font-weight:800; margin-bottom:4px; text-transform:uppercase; letter-spacing:0.5px; color:#c8a45a;">Manual ticket collection office</div>
                                                <div style="color:#cbd5e1; font-size:12px; line-height:18px; margin-bottom:16px; font-weight:500;">{office_address}</div>
                                                <a href="{office_maps_url}" target="_blank" style="display:inline-block; background-color:#1a6b7a; border-radius:10px; color:#ffffff; font-size:12px; line-height:18px; font-weight:800; padding:10px 16px; text-decoration:none; border: 1px solid #1a6b7a;">Open Google Maps</a>
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

        # ── COMMISSION GUARD: Always show public (tourist) prices in email ────
        # The tourist's email must show:
        #   - Amount Paid  = booking.total_amount - booking.remaining_balance
        #   - Balance      = booking.remaining_balance
        # The agent commission is a private internal rebate — NEVER shown to tourist.
        # The booking.total_amount is the TOURIST's full price (before any agent discount).
        # The booking.paid_amount is what the agent actually transferred, which is lower.
        # We do NOT scale anything — we just derive public amounts from the raw booking fields.
        from decimal import Decimal as _Decimal
        _raw_total = _Decimal(str(booking.total_amount))
        _public_remaining = _Decimal(str(booking.remaining_balance))
        _public_paid = _raw_total - _public_remaining

        amount_paid_row = f"""
                        <tr>
                            <td style="padding:16px 20px; border-bottom:1px solid #dbe6ea;">
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#607380; font-size:11px; line-height:16px; font-weight:800; text-transform:uppercase;">Amount paid</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#102f3a; font-size:17px; line-height:25px; font-weight:800;">Rs. {float(_public_paid):.2f}</div>
                            </td>
                        </tr>
        """

        if is_postponement:
            email_type = "POSTPONEMENT"
            subject = "Booking Rescheduled - TS Boat Tourism"
            message_body = f"Your booking <strong style=\"color:#075b60;\">{safe_booking_id}</strong> has been successfully rescheduled to {booking.travel_date.isoformat()}. Your updated official ticket is ready below."
            financial_details = f"""
                        <tr>
                            <td style="padding:16px 20px; border-bottom:1px solid #dbe6ea;">
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#607380; font-size:11px; line-height:16px; font-weight:800; text-transform:uppercase;">Booking status</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#05845f; font-size:17px; line-height:25px; font-weight:800;">Rescheduled</div>
                            </td>
                        </tr>
            """.rstrip()
        elif is_fully_paid:
            email_type = "FULL_PAYMENT"
            subject = "Booking Confirmed - TS Boat Tourism"
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
            subject = "Booking Payment Received - TS Boat Tourism"
            if is_room_booking:
                message_body = f"We received your payment for booking <strong style=\"color:#075b60;\">{safe_booking_id}</strong>. Please clear the remaining balance before your journey and keep the ticket below ready."
            else:
                message_body = f"We received your payment for booking <strong style=\"color:#075b60;\">{safe_booking_id}</strong>. Please clear the remaining balance before your journey and keep the documents below ready."
            financial_details = f"""
                        {amount_paid_row}
                        <tr>
                            <td style="padding:16px 20px;">
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#607380; font-size:11px; line-height:16px; font-weight:800; text-transform:uppercase;">Balance remaining</div>
                                <div style="font-family:Arial, Helvetica, sans-serif; color:#c2410c; font-size:17px; line-height:25px; font-weight:800;">Rs. {float(_public_remaining):.2f}</div>
                            </td>
                        </tr>
            """.rstrip()

        def _generate_html(recipient_name_val):
            return base_html.format(
                subject=escape(subject),
                recipient_name=escape(recipient_name_val),
                message_body=message_body,
                booking_id=safe_booking_id,
                safe_target_label=safe_target_label,
                safe_target_name=safe_target_name,
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


        # Check existing successful emails to prevent duplicates on ARQ retry
        existing_logs_query = select(EmailLog.recipient_email).where(
            EmailLog.booking_id == booking.id,
            EmailLog.email_type == email_type,
            EmailLog.delivery_status == "SENT"
        )
        existing_logs_result = await db.execute(existing_logs_query)
        already_sent_emails = {row[0] for row in existing_logs_result.fetchall() if row[0]}

        # Send to all resolved recipients — pass db so send_booking_email reuses
        # this session instead of opening a new one (prevents connection pool exhaustion).
        for r_email, r_name in recipients:
            if r_email in already_sent_emails:
                logger.info(f"Skipping email to {r_email} because it was already SENT for booking {booking_id}")
                continue

            html = _generate_html(r_name)
            s, err = await email_service.send_booking_email(
                recipient_email=r_email,
                recipient_name=r_name,
                subject=subject,
                html_content=html,
                db=db,
            )
            if not s:
                has_failure = True

            # Log each recipient
            log_entry = EmailLog(
                booking_id=booking.id,
                recipient_email=r_email,
                email_type=email_type,
                delivery_status="SENT" if s else "FAILED",
                failure_reason=err if not s else None
            )
            db.add(log_entry)

        # Send admin notification — reuse same session (Skip if postponement)
        if not is_postponement:
            try:
                admin_success = await send_admin_booking_notification(booking, db=db)
                if not admin_success:
                    has_failure = True
            except Exception as e:
                logger.error(f"Failed to dispatch admin notification: {e}")
                has_failure = True

        await db.commit()
        logger.info(f"process_post_booking_documents_task completed for booking {booking_id}")
        
        if has_failure:
            raise Exception(f"One or more emails failed to send for booking {booking_id}. ARQ will retry.")
