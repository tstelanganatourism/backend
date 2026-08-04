from sqlalchemy import Column, String, Numeric, Integer, Boolean, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from app.models.base import BaseModel

class CheckoutFunnelLog(BaseModel):
    __tablename__ = "checkout_funnel_logs"

    session_id = Column(String, nullable=False, index=True)
    user_id = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    
    # Funnel Stage: CONFIGURING, PASSENGERS_FILLED, CHECKOUT_INITIATED, PAYMENT_ABANDONED, PAYMENT_COMPLETED, MODAL_CLOSED_AFTER_FILL
    funnel_stage = Column(String(50), nullable=False, default="CONFIGURING", index=True)
    
    target_type = Column(String(20), default="package", nullable=False) # 'package' or 'room'
    target_id = Column(Integer, nullable=True)
    target_title = Column(String, nullable=True)
    variant_id = Column(Integer, nullable=True)
    variant_title = Column(String, nullable=True)
    
    travel_date = Column(String, nullable=True)
    adult_count = Column(Integer, default=1, server_default="1", nullable=False)
    child_count = Column(Integer, default=0, server_default="0", nullable=False)
    student_count = Column(Integer, default=0, server_default="0", nullable=False)
    
    total_amount = Column(Numeric(12, 2), nullable=True)
    coupon_code = Column(String(50), nullable=True)
    
    customer_name = Column(String, nullable=True)
    customer_email = Column(String, nullable=True)
    customer_phone = Column(String, nullable=True)
    passengers_data = Column(JSONB, nullable=True)
    
    booking_public_id = Column(String, nullable=True, index=True)
    payment_gateway = Column(String(50), nullable=True)
    abandonment_reason = Column(String, nullable=True)
    
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String, nullable=True)
    
    admin_email_sent = Column(Boolean, default=False, server_default="false", nullable=False)

    __table_args__ = (
        Index("ix_checkout_funnel_logs_session_stage", "session_id", "funnel_stage"),
    )
