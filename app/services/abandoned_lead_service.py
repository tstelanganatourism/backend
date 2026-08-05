import urllib.parse
from typing import Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.models.activity_log import CheckoutFunnelLog
from app.services.email_service import email_service

async def send_admin_abandoned_lead_notification(
    log: CheckoutFunnelLog,
    db: Optional[AsyncSession] = None,
) -> bool:
    """
    Wrapper to handle background execution safely by opening a new session if db is None.
    """
    if db is None:
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as local_db:
            from sqlalchemy import select
            log_res = await local_db.execute(
                select(CheckoutFunnelLog).where(CheckoutFunnelLog.id == log.id)
            )
            local_log = log_res.scalar_one_or_none()
            if not local_log:
                return False
            res = await _send_admin_abandoned_lead_notification_core(local_log, local_db)
            await local_db.commit()
            return res
    else:
        return await _send_admin_abandoned_lead_notification_core(log, db)


async def _send_admin_abandoned_lead_notification_core(
    log: CheckoutFunnelLog,
    db: AsyncSession,
) -> bool:
    """
    Sends an instant, rich, production-grade HTML notification email to the Admin.
    """
    if log.admin_email_sent:
        logger.info(f"Skipping admin abandoned lead email for session {log.session_id} - already sent.")
        return True

    admin_email = "tstelanganatourism@gmail.com"
    
    stage_badges = {
        "CHECKOUT_INITIATED": ("💳 CHECKOUT INITIATED", "#b45309", "#fffbeb", "#fef3c7"),
        "PAYMENT_ABANDONED": ("🚨 PAYMENT ABANDONED / CANCELLED", "#dc2626", "#fef2f2", "#fecaca"),
        "PASSENGERS_FILLED": ("📝 DETAILS FILLED (LEAD CAPTURED)", "#1d4ed8", "#eff6ff", "#bfdbfe"),
        "MODAL_CLOSED_AFTER_FILL": ("🚪 MODAL CLOSED AFTER FILL", "#b45309", "#fffbe0", "#fde68a"),
    }
    
    badge_title, badge_color, badge_bg, badge_border = stage_badges.get(
        log.funnel_stage, 
        (f"🔔 LEAD ACTIVITY: {log.funnel_stage}", "#0d6e75", "#f0fdfa", "#99f6e4")
    )

    cust_name = log.customer_name or "Guest Tourist"
    cust_email = log.customer_email or "Not Provided"
    cust_phone = log.customer_phone or "Not Provided"
    
    target_title = log.target_title or "Tour Package / Stay"
    variant_title = log.variant_title or ""
    travel_date = log.travel_date or "Date Pending"
    
    amount_str = f"₹{float(log.total_amount):,.2f}" if log.total_amount else "N/A"
    
    # Formulate WhatsApp & Call links if phone is available
    clean_phone = ''.join(filter(str.isdigit, str(cust_phone)))
    formatted_phone = cust_phone
    if len(clean_phone) == 10:
        formatted_phone = f"+91 {clean_phone[:5]} {clean_phone[5:]}"
        clean_phone = f"91{clean_phone}"
        
    whatsapp_text = urllib.parse.quote(
        f"Hello {cust_name}, we noticed you were booking {target_title} on TS Boat Tourism. Can we assist you with completing your reservation?"
    )
    whatsapp_url = f"https://wa.me/{clean_phone}?text={whatsapp_text}" if clean_phone else "#"
    call_url = f"tel:+{clean_phone}" if clean_phone else "#"

    # Passengers list HTML
    passengers_html = ""
    if log.passengers_data and isinstance(log.passengers_data, list) and len(log.passengers_data) > 0:
        pass_rows = ""
        for i, p in enumerate(log.passengers_data, 1):
            p_name = p.get("full_name") or p.get("name") or "Passenger"
            p_age = p.get("age") or "-"
            p_gender = p.get("gender") or "-"
            bg_row = "#ffffff" if i % 2 != 0 else "#f8fafc"
            pass_rows += f"""
            <tr style="background-color: {bg_row}; border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px 14px; font-weight: 700; color: #0f172a;">{i}. {p_name}</td>
                <td style="padding: 10px 14px; color: #475569; font-weight: 600;">Age {p_age}</td>
                <td style="padding: 10px 14px; color: #475569; font-weight: 600; text-transform: uppercase; font-size: 11px;">{p_gender}</td>
            </tr>
            """
        passengers_html = f"""
        <div style="margin-top: 22px; background: #ffffff; border: 1px solid #cbd5e1; border-radius: 14px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.03);">
            <div style="background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%); padding: 12px 18px; border-bottom: 1px solid #cbd5e1; font-size: 11px; font-weight: 900; text-transform: uppercase; color: #0d6e75; letter-spacing: 1px;">
                👥 Captured Passenger Manifest ({len(log.passengers_data)} Persons)
            </div>
            <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;">
                {pass_rows}
            </table>
        </div>
        """

    subject = f"⚠️ Abandoned Lead Alert — {cust_name} ({cust_phone})"
    if log.funnel_stage == "PAYMENT_ABANDONED":
        subject = f"🚨 Payment Abandoned — {cust_name} ({amount_str})"
    elif log.funnel_stage == "CHECKOUT_INITIATED":
        subject = f"💳 Payment Pending / Initiated — {cust_name} ({amount_str})"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>{subject}</title>
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #e2e8f0; margin: 0; padding: 24px 12px; -webkit-font-smoothing: antialiased;">
      
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" align="center" style="max-width: 600px; width: 100%; margin: 0 auto; background-color: #ffffff; border-radius: 20px; overflow: hidden; box-shadow: 0 16px 40px rgba(15,23,42,0.12); border: 1px solid #cbd5e1;">
        
        <!-- Header -->
        <tr>
          <td style="background: linear-gradient(135deg, #0f172a 0%, #0d6e75 100%); padding: 30px 32px; text-align: left; border-bottom: 4px solid #f59e0b;">
            <div style="display: inline-block; padding: 6px 16px; border-radius: 50px; font-size: 11px; font-weight: 900; text-transform: uppercase; letter-spacing: 1px; color: {badge_color}; background-color: {badge_bg}; border: 1px solid {badge_border}; margin-bottom: 14px;">
              {badge_title}
            </div>
            <h1 style="margin: 0; font-size: 22px; font-weight: 900; color: #ffffff; letter-spacing: -0.5px; line-height: 1.2;">
              Customer Activity & Lead Alert
            </h1>
            <p style="margin: 6px 0 0 0; font-size: 12px; color: #cbd5e1; font-weight: 500;">
              TS Boat Tourism Automated Real-time Lead Tracking
            </p>
          </td>
        </tr>

        <!-- Content Body -->
        <tr>
          <td style="padding: 28px;">
            
            <!-- Customer Contact Card -->
            <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="width: 100%; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 16px; padding: 20px; margin-bottom: 22px;">
              <tr>
                <td>
                  <div style="font-size: 10px; font-weight: 900; text-transform: uppercase; color: #64748b; letter-spacing: 1.5px; margin-bottom: 6px;">
                    👤 CUSTOMER CONTACT DETAILS
                  </div>
                  <div style="font-size: 18px; font-weight: 900; color: #0f172a; margin-bottom: 14px;">
                    {cust_name}
                  </div>
                  
                  <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="width: 100%; font-size: 13px; color: #334155; margin-bottom: 18px;">
                    <tr>
                      <td style="padding: 4px 0; width: 50%;">
                        <strong style="color: #475569;">📞 Phone:</strong><br>
                        <a href="{call_url}" style="color: #0d6e75 !important; font-weight: 800; text-decoration: none !important; font-size: 14px;">{formatted_phone}</a>
                      </td>
                      <td style="padding: 4px 0; width: 50%;">
                        <strong style="color: #475569;">✉️ Email:</strong><br>
                        <a href="mailto:{cust_email}" style="color: #334155 !important; font-weight: 700; text-decoration: none !important; font-size: 13px;">{cust_email}</a>
                      </td>
                    </tr>
                  </table>
                  
                  {f'''
                  <!-- Action Buttons -->
                  <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                    <tr>
                      <td style="padding-right: 12px;">
                        <a href="{whatsapp_url}" target="_blank" style="display: inline-block; padding: 12px 22px; background-color: #25D366; color: #ffffff !important; text-decoration: none !important; border-radius: 12px; font-weight: 900; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; box-shadow: 0 4px 12px rgba(37,211,102,0.35);">
                          <span style="color: #ffffff !important; text-decoration: none !important;">💬 WHATSAPP LEAD</span>
                        </a>
                      </td>
                      <td>
                        <a href="{call_url}" style="display: inline-block; padding: 12px 22px; background-color: #0d6e75; color: #ffffff !important; text-decoration: none !important; border-radius: 12px; font-weight: 900; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; box-shadow: 0 4px 12px rgba(13,110,117,0.35);">
                          <span style="color: #ffffff !important; text-decoration: none !important;">📞 CALL LEAD</span>
                        </a>
                      </td>
                    </tr>
                  </table>
                  ''' if clean_phone else ''}
                </td>
              </tr>
            </table>

            <!-- Package / Tour Details Card -->
            <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="width: 100%; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 20px; margin-bottom: 22px; box-shadow: 0 4px 12px rgba(0,0,0,0.02);">
              <tr>
                <td>
                  <div style="font-size: 10px; font-weight: 900; text-transform: uppercase; color: #0d6e75; letter-spacing: 1.5px; margin-bottom: 6px;">
                    🗺️ PACKAGE / TOUR SELECTION
                  </div>
                  <div style="font-size: 16px; font-weight: 900; color: #0f172a; margin-bottom: 14px;">
                    {target_title} {f"<span style='color: #64748b; font-weight: 600; font-size: 14px;'>({variant_title})</span>" if variant_title else ""}
                  </div>
                  
                  <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="width: 100%; font-size: 13px; color: #334155; border-top: 1px solid #f1f5f9;">
                    <tr>
                      <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9;"><strong>🗓️ Travel Date:</strong></td>
                      <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; text-align: right; font-weight: 800; color: #0d6e75;">{travel_date}</td>
                    </tr>
                    <tr>
                      <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9;"><strong>👥 Passenger Breakdown:</strong></td>
                      <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; text-align: right; font-weight: 700;">{log.adult_count} Adult(s), {log.child_count} Child(ren)</td>
                    </tr>
                    <tr>
                      <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9;"><strong>💰 Expected Total Amount:</strong></td>
                      <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; text-align: right; font-weight: 900; font-size: 16px; color: #0f172a;">{amount_str}</td>
                    </tr>
                    {f'<tr><td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9;"><strong>🎟️ Coupon Code:</strong></td><td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; text-align: right; font-weight: 800; color: #16a34a;">{log.coupon_code}</td></tr>' if log.coupon_code else ''}
                    {f'<tr><td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9;"><strong>💳 Payment Gateway:</strong></td><td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; text-align: right; font-weight: 800; color: #0f172a;">{log.payment_gateway}</td></tr>' if log.payment_gateway else ''}
                    {f'<tr><td style="padding: 10px 0;"><strong>❌ Failure / Abandon Reason:</strong></td><td style="padding: 10px 0; text-align: right; font-weight: 800; color: #dc2626;">{log.abandonment_reason}</td></tr>' if log.abandonment_reason else ''}
                  </table>
                </td>
              </tr>
            </table>

            {passengers_html}

          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background-color: #f8fafc; padding: 20px 32px; border-top: 1px solid #e2e8f0; font-size: 11px; color: #94a3b8; text-align: center; line-height: 1.5;">
            Session ID: <strong style="color: #64748b;">{log.session_id}</strong> • IP: <strong style="color: #64748b;">{log.ip_address or 'Unknown'}</strong><br>
            TS Boat Tourism Automated Real-time Lead & Abandonment Tracking
          </td>
        </tr>

      </table>

    </body>
    </html>
    """

    success, error = await email_service.send_booking_email(
        recipient_email=admin_email,
        recipient_name="TS Boat Tourism Admin",
        subject=subject,
        html_content=html_content,
        is_admin=True,
        db=db,
    )

    if success:
        log.admin_email_sent = True
        logger.info(f"Admin abandoned lead email successfully sent for session {log.session_id} (Lead: {cust_name})")
    else:
        logger.error(f"Failed to send admin abandoned lead email for session {log.session_id}: {error}")

    return success
