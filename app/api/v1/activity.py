from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Request, status, BackgroundTasks
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from loguru import logger

from app.db.session import get_db
from app.models.activity_log import CheckoutFunnelLog
from app.models.user import User
from app.middleware.auth import get_current_user_optional, require_admin
from app.services.abandoned_lead_service import send_admin_abandoned_lead_notification

router = APIRouter()

class FunnelEventSchema(BaseModel):
    session_id: str
    funnel_stage: str  # CONFIGURING, PASSENGERS_FILLED, CHECKOUT_INITIATED, PAYMENT_ABANDONED, PAYMENT_COMPLETED, MODAL_CLOSED_AFTER_FILL
    target_type: str = "package"
    target_id: Optional[int] = None
    target_title: Optional[str] = None
    variant_id: Optional[int] = None
    variant_title: Optional[str] = None
    travel_date: Optional[str] = None
    adult_count: int = 1
    child_count: int = 0
    student_count: int = 0
    total_amount: Optional[float] = None
    coupon_code: Optional[str] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    passengers_data: Optional[List[Dict[str, Any]]] = None
    booking_public_id: Optional[str] = None
    payment_gateway: Optional[str] = None
    abandonment_reason: Optional[str] = None

@router.post("/funnel-event")
async def track_funnel_event(
    payload: FunnelEventSchema,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Logs or updates a user's checkout funnel activity, capturing customer info,
    selected packages/dates, filled passenger details, and abandonment stages.
    """
    try:
        query = select(CheckoutFunnelLog).where(CheckoutFunnelLog.session_id == payload.session_id)
        result = await db.execute(query)
        log = result.scalars().first()

        if not log:
            log = CheckoutFunnelLog(
                session_id=payload.session_id,
                funnel_stage=payload.funnel_stage,
                target_type=payload.target_type,
                target_id=payload.target_id,
                target_title=payload.target_title,
                variant_id=payload.variant_id,
                variant_title=payload.variant_title,
                travel_date=payload.travel_date,
                adult_count=payload.adult_count,
                child_count=payload.child_count,
                student_count=payload.student_count,
                total_amount=payload.total_amount,
                coupon_code=payload.coupon_code,
                customer_name=payload.customer_name,
                customer_email=payload.customer_email,
                customer_phone=payload.customer_phone,
                passengers_data=payload.passengers_data,
                booking_public_id=payload.booking_public_id,
                payment_gateway=payload.payment_gateway,
                abandonment_reason=payload.abandonment_reason,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            if current_user:
                log.user_id = current_user.id
                if not log.customer_name and current_user.full_name:
                    log.customer_name = current_user.full_name
                if not log.customer_email and current_user.email:
                    log.customer_email = current_user.email
                if not log.customer_phone and current_user.phone_number:
                    log.customer_phone = current_user.phone_number
            db.add(log)
        else:
            # Update existing log for this session
            log.funnel_stage = payload.funnel_stage
            if payload.target_type: log.target_type = payload.target_type
            if payload.target_id: log.target_id = payload.target_id
            if payload.target_title: log.target_title = payload.target_title
            if payload.variant_id: log.variant_id = payload.variant_id
            if payload.variant_title: log.variant_title = payload.variant_title
            if payload.travel_date: log.travel_date = payload.travel_date
            
            log.adult_count = payload.adult_count
            log.child_count = payload.child_count
            log.student_count = payload.student_count
            
            if payload.total_amount is not None: log.total_amount = payload.total_amount
            if payload.coupon_code: log.coupon_code = payload.coupon_code
            if payload.customer_name: log.customer_name = payload.customer_name
            if payload.customer_email: log.customer_email = payload.customer_email
            if payload.customer_phone: log.customer_phone = payload.customer_phone
            if payload.passengers_data is not None: log.passengers_data = payload.passengers_data
            if payload.booking_public_id: log.booking_public_id = payload.booking_public_id
            if payload.payment_gateway: log.payment_gateway = payload.payment_gateway
            if payload.abandonment_reason: log.abandonment_reason = payload.abandonment_reason

            if current_user and not log.user_id:
                log.user_id = current_user.id
                if not log.customer_name and current_user.full_name:
                    log.customer_name = current_user.full_name
                if not log.customer_email and current_user.email:
                    log.customer_email = current_user.email
                if not log.customer_phone and current_user.phone_number:
                    log.customer_phone = current_user.phone_number

        await db.commit()
        await db.refresh(log)

        # Trigger admin notification email for critical lead/abandonment stages
        should_notify_admin = (
            (payload.funnel_stage in ["PASSENGERS_FILLED", "CHECKOUT_INITIATED", "PAYMENT_ABANDONED", "MODAL_CLOSED_AFTER_FILL"])
            and (log.customer_phone or log.customer_email)
            and not log.admin_email_sent
        )
        
        # If it's explicitly a PAYMENT_ABANDONED event, notify even if previously notified as PASSENGERS_FILLED
        if payload.funnel_stage == "PAYMENT_ABANDONED":
            log.admin_email_sent = False  # Reset flag to force send updated abandoned email
            should_notify_admin = True

        if should_notify_admin:
            try:
                await send_admin_abandoned_lead_notification(log, db)
                await db.commit()
            except Exception as mail_err:
                logger.error(f"Failed to dispatch admin lead notification: {mail_err}")

        return {
            "status": "success",
            "session_id": log.session_id,
            "funnel_stage": log.funnel_stage,
            "admin_notified": log.admin_email_sent
        }
    except Exception as e:
        logger.error(f"Error tracking funnel event: {e}")
        await db.rollback()
        return {
            "status": "ignored",
            "message": str(e)
        }

@router.get("/admin/leads")
async def get_admin_leads(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    """
    Returns recent checkout funnel logs & abandoned leads for the admin panel.
    """
    query = select(CheckoutFunnelLog).order_by(desc(CheckoutFunnelLog.updated_at)).limit(limit)
    result = await db.execute(query)
    logs = result.scalars().all()
    
    return [
        {
            "id": l.id,
            "session_id": l.session_id,
            "user_id": l.user_id,
            "funnel_stage": l.funnel_stage,
            "target_type": l.target_type,
            "target_title": l.target_title,
            "variant_title": l.variant_title,
            "travel_date": l.travel_date,
            "adult_count": l.adult_count,
            "child_count": l.child_count,
            "total_amount": float(l.total_amount) if l.total_amount else None,
            "customer_name": l.customer_name,
            "customer_email": l.customer_email,
            "customer_phone": l.customer_phone,
            "passengers_count": len(l.passengers_data) if l.passengers_data else 0,
            "admin_email_sent": l.admin_email_sent,
            "updated_at": l.updated_at.isoformat() if l.updated_at else None,
        }
        for l in logs
    ]
