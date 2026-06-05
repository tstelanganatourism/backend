import hmac
import hashlib
from typing import Optional
from decimal import Decimal
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import settings
from app.models.booking import Booking
from app.services.email_service import email_service


def _booking_hash(public_id: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        public_id.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


async def send_admin_booking_notification(
    booking: Booking,
    db: Optional[AsyncSession] = None,
):
    """
    Send a beautifully styled admin notification email for every new booking.
    Shows full financial breakdown (transport, refreshments, GST, gateway fee),
    real agent commission/net payable, real captured payment amount from the
    payment ledger, document links (Ticket / Invoice / Form), and passenger list.
    """
    admin_email = "tsboattourismservices@gmail.com"
    subject = f"🆕 New Booking — {booking.public_id}"

    pricing = booking.pricing_snapshot or {}

    # ── 1. Core booking meta ──────────────────────────────────────────────────
    is_room = booking.room_variant_id is not None
    booking_type_label = "Accommodation / Room" if is_room else "Package / Tour"

    frontend_url = settings.FRONTEND_URL.rstrip("/")
    secret = _booking_hash(booking.public_id)
    ticket_url   = f"{frontend_url}/print/ticket/{booking.public_id}?secret={secret}"
    invoice_url  = f"{frontend_url}/print/invoice/{booking.public_id}?secret={secret}"
    form_url     = f"{frontend_url}/print/form/{booking.public_id}?secret={secret}" if not is_room else ""
    dashboard_url = f"{frontend_url}/admin/bookings"

    # ── 2. Primary contact ────────────────────────────────────────────────────
    primary_name  = "Guest"
    primary_phone = "—"
    if booking.passengers:
        for p in booking.passengers:
            if p.is_primary:
                primary_name  = p.full_name
                primary_phone = p.phone_number or "—"
                break
        if primary_name == "Guest" and booking.passengers:
            primary_name  = booking.passengers[0].full_name
            primary_phone = booking.passengers[0].phone_number or "—"

    # ── 3. Passenger roster HTML ──────────────────────────────────────────────
    passenger_rows = ""
    if booking.passengers:
        for idx, p in enumerate(booking.passengers, 1):
            gender = (p.gender.value if p.gender else "—").capitalize()
            badge_color = "#0d9488" if p.is_primary else "#64748b"
            primary_badge = (
                '<span style="background:#e0f2fe;color:#0369a1;font-size:9px;'
                'font-weight:700;padding:2px 6px;border-radius:10px;margin-left:6px;'
                'text-transform:uppercase;letter-spacing:.5px;">PRIMARY</span>'
                if p.is_primary else ""
            )
            passenger_rows += f"""
            <tr>
              <td style="padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#1e293b;">
                <table cellpadding="0" cellspacing="0" border="0" style="margin:0;padding:0;">
                  <tr>
                    <td style="width:22px;height:22px;background:{badge_color};color:#fff;text-align:center;vertical-align:middle;border-radius:50%;font-size:10px;font-weight:700;">{idx}</td>
                    <td style="padding-left:8px;vertical-align:middle;"><strong>{p.full_name}</strong>{primary_badge}</td>
                  </tr>
                </table>
              </td>
              <td style="padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;text-align:center;">{p.age} yrs</td>
              <td style="padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;text-align:center;">{gender}</td>
            </tr>"""

    # ── 4. Travel date / room dates ───────────────────────────────────────────
    if is_room and pricing.get("slot_start"):
        date_label = "Check-in"
        date_value = f"{pricing.get('slot_start')} → {pricing.get('slot_end')}"
        date_extra = f"<br><small style='color:#64748b;'>Travel date: {booking.travel_date}</small>"
    else:
        date_label = "Travel Date"
        date_value = str(booking.travel_date)
        date_extra = ""

    adult_count = booking.adult_count or 0
    child_count = booking.child_count or 0
    total_guests = adult_count + child_count

    # ── 5. Financial line items ───────────────────────────────────────────────
    subtotal_base   = Decimal(str(pricing.get("subtotal_amount", "0.00")))
    refreshment_sub = Decimal(str(pricing.get("refreshment_subtotal", "0.00")))
    gst_amount      = Decimal(str(pricing.get("gst_amount", "0.00")))
    gateway_fee     = Decimal(str(pricing.get("gateway_fee", "0.00")))
    tourist_total   = Decimal(str(pricing.get("tourist_total", "0.00")))
    coupon_discount = Decimal(str(pricing.get("coupon_discount", "0.00")))
    payment_pct     = pricing.get("payment_percentage", "100")

    # Base fare = subtotal minus refreshment and transport (snap)
    transport_cost = Decimal("0.00")
    transport_rows_html = ""
    if pricing.get("transport_selections"):
        for t in pricing["transport_selections"]:
            # KEY FIX: use item_total, not subtotal
            cost = Decimal(str(t.get("item_total", t.get("subtotal", 0))))
            transport_cost += cost
            t_title  = t.get("title", "Transport")
            t_type   = "Separate Vehicle" if t.get("type") == "SEPARATE_VEHICLE" else "Shared Transport"
            t_qty    = t.get("quantity", 1)
            transport_rows_html += f"""
            <tr>
              <td style="padding:6px 0;font-size:13px;color:#475569;">
                {t_title}
                <small style="display:block;color:#94a3b8;font-size:11px;">{t_type} × {t_qty}</small>
              </td>
              <td style="padding:6px 0;font-size:13px;color:#1e293b;text-align:right;font-weight:600;">₹{float(cost):,.2f}</td>
            </tr>"""

    base_fare = subtotal_base - refreshment_sub - transport_cost

    fin_rows = f"""
    <tr>
      <td style="padding:8px 0;font-size:13px;color:#475569;">Package / Base Fare</td>
      <td style="padding:8px 0;font-size:13px;color:#1e293b;text-align:right;font-weight:500;">₹{float(base_fare):,.2f}</td>
    </tr>"""

    if float(transport_cost) > 0:
        fin_rows += f"""
    <tr>
      <td style="padding:4px 0 0 0;font-size:12px;color:#94a3b8;font-weight:600;text-transform:uppercase;letter-spacing:.5px;"
          colspan="2">Transport</td>
    </tr>
    {transport_rows_html}"""

    if float(refreshment_sub) > 0:
        fin_rows += f"""
    <tr>
      <td style="padding:6px 0;font-size:13px;color:#475569;">Refreshments</td>
      <td style="padding:6px 0;font-size:13px;color:#1e293b;text-align:right;font-weight:500;">₹{float(refreshment_sub):,.2f}</td>
    </tr>"""

    if float(coupon_discount) > 0:
        fin_rows += f"""
    <tr>
      <td style="padding:6px 0;font-size:13px;color:#059669;">Coupon Discount ({pricing.get("coupon_applied","")})</td>
      <td style="padding:6px 0;font-size:13px;color:#059669;text-align:right;font-weight:600;">−₹{float(coupon_discount):,.2f}</td>
    </tr>"""

    fin_rows += f"""
    <tr>
      <td style="padding:6px 0;font-size:13px;color:#475569;">GST (5%)</td>
      <td style="padding:6px 0;font-size:13px;color:#1e293b;text-align:right;font-weight:500;">₹{float(gst_amount):,.2f}</td>
    </tr>
    <tr>
      <td style="padding:6px 0;font-size:13px;color:#475569;">Gateway Fee (1%)</td>
      <td style="padding:6px 0;font-size:13px;color:#1e293b;text-align:right;font-weight:500;">₹{float(gateway_fee):,.2f}</td>
    </tr>"""

    # ── 6. Grand total row ────────────────────────────────────────────────────
    grand_total_row = f"""
    <tr>
      <td style="padding:12px 0 8px 0;font-size:15px;font-weight:700;color:#0f172a;
          border-top:2px solid #e2e8f0;">Grand Invoice Total</td>
      <td style="padding:12px 0 8px 0;font-size:15px;font-weight:700;color:#0f172a;
          text-align:right;border-top:2px solid #e2e8f0;">₹{float(tourist_total):,.2f}</td>
    </tr>"""

    # ── 7. Agent section ──────────────────────────────────────────────────────
    agent_section_html = ""
    is_agent_booking = booking.agent_id is not None

    agent_name    = "—"
    agent_phone   = "—"
    agent_email   = "—"
    agent_company = "—"

    if is_agent_booking:
        # Fetch agent user safely
        try:
            if db is not None:
                from app.models.user import User
                agent_res = await db.execute(
                    select(User).where(User.id == booking.agent_id)
                )
                agent_user = agent_res.scalar_one_or_none()
                if agent_user:
                    agent_name    = agent_user.full_name or "—"
                    agent_phone   = agent_user.phone_number or "—"
                    agent_email   = agent_user.email or "—"
                    agent_company = agent_user.company_name or "—"
            else:
                # Fallback from pricing snapshot metadata
                meta = pricing.get("agent_metadata", {})
                agent_name = meta.get("agent_name", "—")
        except Exception as e:
            logger.warning(f"Could not fetch agent details for booking {booking.public_id}: {e}")

        agent_commission = Decimal(str(booking.agent_commission or "0.00"))
        agent_net_payable = max(Decimal("0.00"), tourist_total - agent_commission)

        # Real amounts actually captured from ledger
        real_paid      = Decimal(str(pricing.get("actual_paid_advance", str(booking.paid_amount))))
        real_remaining = max(Decimal("0.00"), agent_net_payable - real_paid)

        commission_pct_meta = pricing.get("agent_metadata", {}).get("commission_percentage", "")
        commission_label = (
            f"Agent Commission ({commission_pct_meta}%)" if commission_pct_meta else "Agent Commission"
        )

        agent_section_html = f"""
        <!-- AGENT SECTION -->
        <tr>
          <td style="padding:0 32px 0 32px;">
            <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:12px;
                padding:20px 24px;margin-bottom:24px;">
              <div style="font-size:12px;font-weight:700;color:#92400e;text-transform:uppercase;
                  letter-spacing:.8px;margin-bottom:14px;">🤝 Agent Booking</div>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:14px;">
                <tr>
                  <td style="padding:4px 0;font-size:13px;color:#78350f;width:38%;">Agent Name</td>
                  <td style="padding:4px 0;font-size:13px;color:#1c1917;font-weight:600;">{agent_name}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;font-size:13px;color:#78350f;">Phone</td>
                  <td style="padding:4px 0;font-size:13px;color:#1c1917;font-weight:600;">{agent_phone}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;font-size:13px;color:#78350f;">Email</td>
                  <td style="padding:4px 0;font-size:13px;color:#1c1917;font-weight:600;">{agent_email}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;font-size:13px;color:#78350f;">Company</td>
                  <td style="padding:4px 0;font-size:13px;color:#1c1917;font-weight:600;">{agent_company}</td>
                </tr>
              </table>
              <table width="100%" cellpadding="0" cellspacing="0" border="0"
                  style="border-top:1px dashed #fbbf24;padding-top:12px;">
                <tr>
                  <td style="padding:5px 0;font-size:13px;color:#78350f;">{commission_label}</td>
                  <td style="padding:5px 0;font-size:13px;color:#dc2626;text-align:right;font-weight:700;">
                    −₹{float(agent_commission):,.2f}
                  </td>
                </tr>
                <tr>
                  <td style="padding:5px 0;font-size:14px;font-weight:700;color:#1c1917;">Agent Net Payable</td>
                  <td style="padding:5px 0;font-size:14px;font-weight:700;color:#1c1917;text-align:right;">
                    ₹{float(agent_net_payable):,.2f}
                  </td>
                </tr>
              </table>
              <!-- Payment Progress -->
              <div style="margin-top:14px;">
                <div style="font-size:11px;font-weight:700;color:#92400e;text-transform:uppercase;
                    letter-spacing:.6px;margin-bottom:6px;">Payment Progress (Agent Cash)</div>
                <table width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td width="50%" style="background:#d1fae5;border-radius:6px 0 0 6px;
                        padding:8px 12px;text-align:center;">
                      <div style="font-size:10px;font-weight:700;color:#065f46;text-transform:uppercase;">Received</div>
                      <div style="font-size:16px;font-weight:800;color:#065f46;">₹{float(real_paid):,.2f}</div>
                    </td>
                    <td width="50%" style="background:#fef3c7;border-radius:0 6px 6px 0;
                        padding:8px 12px;text-align:center;">
                      <div style="font-size:10px;font-weight:700;color:#92400e;text-transform:uppercase;">Balance Due</div>
                      <div style="font-size:16px;font-weight:800;color:#92400e;">₹{float(real_remaining):,.2f}</div>
                    </td>
                  </tr>
                </table>
              </div>
            </div>
          </td>
        </tr>"""
    else:
        # Public booking — show tourist paid / remaining
        real_paid_pub      = Decimal(str(booking.paid_amount or "0.00"))
        real_remaining_pub = Decimal(str(booking.remaining_balance or "0.00"))

        agent_section_html = f"""
        <!-- PUBLIC PAYMENT PROGRESS -->
        <tr>
          <td style="padding:0 32px 24px 32px;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td width="50%" style="background:#d1fae5;border-radius:6px 0 0 6px;
                    padding:10px 14px;text-align:center;">
                  <div style="font-size:10px;font-weight:700;color:#065f46;text-transform:uppercase;">Amount Paid</div>
                  <div style="font-size:18px;font-weight:800;color:#065f46;">₹{float(real_paid_pub):,.2f}</div>
                </td>
                <td width="50%" style="background:#fee2e2;border-radius:0 6px 6px 0;
                    padding:10px 14px;text-align:center;">
                  <div style="font-size:10px;font-weight:700;color:#991b1b;text-transform:uppercase;">Balance Due</div>
                  <div style="font-size:18px;font-weight:800;color:#991b1b;">₹{float(real_remaining_pub):,.2f}</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    # ── 8. Document action buttons ────────────────────────────────────────────
    doc_buttons = f"""
    <a href="{ticket_url}" target="_blank"
       style="display:inline-block;background:#0f766e;color:#fff;text-decoration:none;
       padding:10px 18px;border-radius:8px;font-size:13px;font-weight:700;margin:4px;">
       🎫 View Ticket
    </a>
    <a href="{invoice_url}" target="_blank"
       style="display:inline-block;background:#1d4ed8;color:#fff;text-decoration:none;
       padding:10px 18px;border-radius:8px;font-size:13px;font-weight:700;margin:4px;">
       🧾 View Invoice
    </a>"""
    if form_url:
        doc_buttons += f"""
    <a href="{form_url}" target="_blank"
       style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;
       padding:10px 18px;border-radius:8px;font-size:13px;font-weight:700;margin:4px;">
       📋 Print Form
    </a>"""

    # ── 9. Status badge ───────────────────────────────────────────────────────
    status_str = str(booking.status.value if hasattr(booking.status, "value") else booking.status).replace("_", " ")
    if "FULLY" in status_str.upper():
        badge_bg, badge_col = "#dcfce7", "#166534"
    elif "PARTIAL" in status_str.upper():
        badge_bg, badge_col = "#fef3c7", "#92400e"
    else:
        badge_bg, badge_col = "#f1f5f9", "#475569"

    source_label = "Agent Booking" if is_agent_booking else "Public Booking"
    source_badge_bg = "#fef3c7" if is_agent_booking else "#eff6ff"
    source_badge_col = "#92400e" if is_agent_booking else "#1d4ed8"

    # ── 10. Full HTML email ───────────────────────────────────────────────────
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>New Booking — {booking.public_id}</title>
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:#f0f4f8;min-height:100vh;">
  <tr>
    <td align="center" style="padding:32px 16px;">

      <!-- CARD -->
      <table width="100%" cellpadding="0" cellspacing="0" border="0"
             style="max-width:620px;background:#ffffff;border-radius:16px;
             overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.10);">

        <!-- ===== HEADER ===== -->
        <tr>
          <td style="background:linear-gradient(135deg,#0f766e 0%,#0d5a5a 100%);
              padding:28px 32px;">
            <div style="font-size:11px;font-weight:700;color:#99f6e4;
                text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;">
              TS Boat Tourism — Admin Notification
            </div>
            <div style="font-size:26px;font-weight:800;color:#ffffff;
                line-height:1.2;margin-bottom:12px;">
              🆕 New Booking Received
            </div>
            <table cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="background:rgba(255,255,255,.15);border-radius:20px;
                    padding:5px 14px;margin-right:8px;">
                  <span style="font-size:13px;font-weight:700;color:#fff;
                      letter-spacing:.5px;">{booking.public_id}</span>
                </td>
                <td style="width:8px;"></td>
                <td style="background:{badge_bg};border-radius:20px;padding:5px 14px;">
                  <span style="font-size:12px;font-weight:700;color:{badge_col};">
                    {status_str}
                  </span>
                </td>
                <td style="width:8px;"></td>
                <td style="background:{source_badge_bg};border-radius:20px;padding:5px 14px;">
                  <span style="font-size:12px;font-weight:700;color:{source_badge_col};">
                    {source_label}
                  </span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- ===== CUSTOMER DETAILS ===== -->
        <tr>
          <td style="padding:24px 32px 0 32px;">
            <div style="font-size:11px;font-weight:700;color:#94a3b8;
                text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">
              Customer Details
            </div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="background:#f8fafc;border-radius:10px;padding:16px 20px;">
              <tr>
                <td style="padding:5px 0;font-size:13px;color:#64748b;width:35%;">Name</td>
                <td style="padding:5px 0;font-size:13px;color:#0f172a;font-weight:700;">{primary_name}</td>
              </tr>
              <tr>
                <td style="padding:5px 0;font-size:13px;color:#64748b;">Phone</td>
                <td style="padding:5px 0;font-size:13px;color:#0f172a;font-weight:600;">{primary_phone}</td>
              </tr>
              <tr>
                <td style="padding:5px 0;font-size:13px;color:#64748b;">{date_label}</td>
                <td style="padding:5px 0;font-size:13px;color:#0f172a;font-weight:600;">
                  {date_value}{date_extra}
                </td>
              </tr>
              <tr>
                <td style="padding:5px 0;font-size:13px;color:#64748b;">Guests</td>
                <td style="padding:5px 0;font-size:13px;color:#0f172a;font-weight:600;">
                  {total_guests} total
                  <span style="color:#64748b;font-weight:400;">
                    ({adult_count} adult{'s' if adult_count != 1 else ''}, {child_count} child{'ren' if child_count != 1 else ''})
                  </span>
                </td>
              </tr>
              <tr>
                <td style="padding:5px 0;font-size:13px;color:#64748b;">Booking Type</td>
                <td style="padding:5px 0;font-size:13px;color:#0f172a;font-weight:600;">{booking_type_label}</td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- ===== PASSENGER ROSTER ===== -->
        <tr>
          <td style="padding:20px 32px 0 32px;">
            <div style="font-size:11px;font-weight:700;color:#94a3b8;
                text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;">
              Passenger Roster
            </div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">
              <thead>
                <tr style="background:#f1f5f9;">
                  <th style="padding:8px 12px;font-size:11px;font-weight:700;color:#64748b;
                      text-align:left;text-transform:uppercase;letter-spacing:.5px;">Name</th>
                  <th style="padding:8px 12px;font-size:11px;font-weight:700;color:#64748b;
                      text-align:center;text-transform:uppercase;letter-spacing:.5px;">Age</th>
                  <th style="padding:8px 12px;font-size:11px;font-weight:700;color:#64748b;
                      text-align:center;text-transform:uppercase;letter-spacing:.5px;">Gender</th>
                </tr>
              </thead>
              <tbody>
                {passenger_rows}
              </tbody>
            </table>
          </td>
        </tr>

        <!-- ===== FINANCIAL BREAKDOWN ===== -->
        <tr>
          <td style="padding:20px 32px 0 32px;">
            <div style="font-size:11px;font-weight:700;color:#94a3b8;
                text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;">
              Financial Breakdown
            </div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="background:#f8fafc;border-radius:10px;padding:16px 20px;">
              {fin_rows}
              {grand_total_row}
            </table>
          </td>
        </tr>

        <!-- SPACER -->
        <tr><td style="padding:20px 0 0 0;"></td></tr>

        <!-- ===== AGENT / PAYMENT SECTION ===== -->
        {agent_section_html}

        <!-- ===== DOCUMENT LINKS ===== -->
        <tr>
          <td style="padding:0 32px 24px 32px;">
            <div style="font-size:11px;font-weight:700;color:#94a3b8;
                text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">
              Booking Documents
            </div>
            <div style="text-align:left;">
              {doc_buttons}
            </div>
          </td>
        </tr>

        <!-- ===== ADMIN DASHBOARD BUTTON ===== -->
        <tr>
          <td style="padding:0 32px 32px 32px;">
            <a href="{dashboard_url}" target="_blank"
               style="display:block;background:#0f172a;color:#fff;text-decoration:none;
               text-align:center;padding:14px 24px;border-radius:10px;font-size:14px;
               font-weight:700;letter-spacing:.3px;">
              🖥️ Open Admin Dashboard
            </a>
          </td>
        </tr>

        <!-- ===== FOOTER ===== -->
        <tr>
          <td style="background:#f8fafc;border-top:1px solid #e2e8f0;
              padding:16px 32px;text-align:center;">
            <div style="font-size:11px;color:#94a3b8;line-height:1.6;">
              Automated notification from <strong>TS Boat Tourism Platform</strong>.
              This email is intended for admin use only.
            </div>
          </td>
        </tr>

      </table>
      <!-- /CARD -->
    </td>
  </tr>
</table>

</body>
</html>"""

    success, error = await email_service.send_booking_email(
        recipient_email=admin_email,
        recipient_name="TS Tours Admin",
        subject=subject,
        html_content=html_content,
        is_admin=True,
    )

    if not success:
        logger.error(
            f"Failed to send admin notification for booking {booking.public_id}: {error}"
        )
    else:
        logger.info(
            f"Admin notification sent successfully for booking {booking.public_id}"
        )

    return success
