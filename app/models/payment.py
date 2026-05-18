from sqlalchemy import Column, String, Numeric, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import PaymentStatus

class Payment(BaseModel):
    __tablename__ = "payments"

    booking_id = Column(ForeignKey("bookings.id"), nullable=False, index=True)
    razorpay_order_id = Column(String, unique=True, index=True, nullable=False)
    razorpay_payment_id = Column(String, unique=True, index=True, nullable=True)
    razorpay_signature = Column(String(255), nullable=True)
    
    amount = Column(Numeric(12, 2), nullable=False)
    status = Column(SQLEnum(PaymentStatus), default=PaymentStatus.CREATED, server_default="CREATED", nullable=False, index=True)
    payment_method = Column(String, nullable=True)
    
    error_code = Column(String(100), nullable=True)
    error_description = Column(String, nullable=True)

    booking = relationship("Booking", back_populates="payments")
