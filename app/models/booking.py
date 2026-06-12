from sqlalchemy import Column, String, Numeric, Integer, Boolean, ForeignKey, CheckConstraint, Computed, Enum as SQLEnum, DateTime, Date, Index, UniqueConstraint, func
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from app.models.base import BaseModel
from app.models.enums import BookingStatus, CancellationStatus, GenderType, BookingSource, DocumentGenerationStatus

class Booking(BaseModel):
    __tablename__ = "bookings"

    public_id = Column(String, unique=True, nullable=False, index=True)
    user_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    agent_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    source = Column(SQLEnum(BookingSource), default=BookingSource.PUBLIC, server_default="PUBLIC", nullable=False)
    customer_email = Column(String, nullable=True)  # Tourist email for direct/agent bookings

    # Strict Mutually Exclusive Foreign Keys
    room_variant_id = Column(ForeignKey("room_variants.id", ondelete="RESTRICT"), nullable=True, index=True)
    variant_id = Column(ForeignKey("package_variants.id", ondelete="RESTRICT"), nullable=True, index=True)

    __table_args__ = (
        CheckConstraint(
            "(room_variant_id IS NOT NULL AND variant_id IS NULL) OR (room_variant_id IS NULL AND variant_id IS NOT NULL)",
            name="chk_booking_target"
        ),
        Index("ix_bookings_created_at", "created_at"),
    )

    travel_date = Column(Date, nullable=False, index=True)
    adult_count = Column(Integer, default=1, server_default="1", nullable=False)
    child_count = Column(Integer, default=0, server_default="0", nullable=False)
    student_count = Column(Integer, default=0, server_default="0", nullable=False)  # for student packages
    has_refreshment_addon = Column(Boolean, default=False, server_default="false", nullable=False)

    # Monetary Calculations (BR-08 / BR-09 / BR-10 / BR-11)
    subtotal_amount = Column(Numeric(12, 2), nullable=False)
    coupon_discount = Column(Numeric(12, 2), default=0.00, server_default="0.00", nullable=False)
    coupon_applied = Column(String(50), nullable=True)
    gst_amount = Column(Numeric(12, 2), nullable=False)       # Fixed 5% GST
    gateway_fee = Column(Numeric(12, 2), nullable=False)      # Fixed 1% Gateway Fee
    total_amount = Column(Numeric(12, 2), nullable=False)     # Grand total
    
    # Partial Payment Support
    paid_amount = Column(Numeric(12, 2), default=0.00, server_default="0.00", nullable=False)
    remaining_balance = Column(Numeric(12, 2), nullable=False) # total_amount - paid_amount

    # Commission Tracking (Strictly Hidden)
    agent_commission = Column(Numeric(12, 2), default=0.00, server_default="0.00", nullable=False)

    status = Column(SQLEnum(BookingStatus), default=BookingStatus.PENDING, server_default="PENDING", nullable=False, index=True)
    pricing_snapshot = Column(JSONB, nullable=True)

    # Document & Storage Architecture fields (Private R2)
    ticket_pdf_url = Column(String, nullable=True)
    invoice_pdf_url = Column(String, nullable=True)
    ticket_generation_status = Column(SQLEnum(DocumentGenerationStatus), default=DocumentGenerationStatus.MISSING, server_default="MISSING", nullable=False)
    invoice_generation_status = Column(SQLEnum(DocumentGenerationStatus), default=DocumentGenerationStatus.MISSING, server_default="MISSING", nullable=False)

    passengers = relationship("BookingPassenger", back_populates="booking", cascade="all, delete-orphan")
    stay_dates = relationship("BookingStayDate", back_populates="booking", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="booking", cascade="all, delete-orphan")
    cancellation_requests = relationship("CancellationRequest", back_populates="booking", cascade="all, delete-orphan")
    agent = relationship("User", foreign_keys=[agent_id], back_populates="agent_bookings")
    customer = relationship("User", foreign_keys=[user_id])
    package_variant = relationship("PackageVariant", foreign_keys=[variant_id])
    room_variant = relationship("RoomVariant", foreign_keys=[room_variant_id])

class BookingStayDate(BaseModel):
    __tablename__ = "booking_stay_dates"

    booking_id = Column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint('booking_id', 'date', name='uq_booking_stay_date'),
    )

    booking = relationship("Booking", back_populates="stay_dates")

class BookingPassenger(BaseModel):
    __tablename__ = "booking_passengers"

    booking_id = Column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    full_name = Column(String, nullable=False)
    age = Column(Integer, nullable=False)
    gender = Column(SQLEnum(GenderType), nullable=True)
    is_child = Column(Boolean, Computed("age <= 10", persisted=True))
    phone_number = Column(String, nullable=True)
    relationship_to_lead = Column(String(50), nullable=True)
    is_primary = Column(Boolean, default=False, server_default="false", nullable=False)
    student_class = Column(String(100), nullable=True)  # free-text class for student packages (e.g. "Class 5", "Inter 1st Year")
    
    # Secure Encrypted Aadhaar fields (BR-12) — nullable for children (<18)
    aadhar_encrypted = Column(String(512), nullable=True)
    aadhar_hash = Column(String(64), nullable=True, index=True)
    aadhar_image_url = Column(String(512), nullable=True)

    booking = relationship("Booking", back_populates="passengers")

class CancellationRequest(BaseModel):
    __tablename__ = "cancellation_requests"

    booking_id = Column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    reason = Column(String, nullable=False)
    status = Column(SQLEnum(CancellationStatus), default=CancellationStatus.PENDING, server_default="PENDING", nullable=False)
    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    processed_by = Column(ForeignKey("users.id"), nullable=True)
    admin_notes = Column(String, nullable=True)
    
    # Store cancellation metadata (Phase 2)
    cancellation_fee = Column(Numeric(12, 2), nullable=True)
    refund_amount = Column(Numeric(12, 2), nullable=True)

    booking = relationship("Booking", back_populates="cancellation_requests")

class BookingDraft(BaseModel):
    """
    Temporary hold for checkouts. Converted to Booking only on payment webhook success.
    Works for both PhonePe and Cashfree gateways.
    """
    __tablename__ = "booking_drafts"

    draft_id = Column(String, unique=True, nullable=False, index=True)
    pg_transaction_id = Column(String, unique=True, nullable=True, index=True)  # PhonePe: merchant_txn_id | Cashfree: order_id
    payment_gateway = Column(String(20), nullable=True, server_default="PHONEPE")  # PHONEPE | CASHFREE

    user_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    agent_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True)
    
    # Store the entire validated request payload and pricing snapshot
    checkout_payload = Column(JSONB, nullable=False)
    pricing_snapshot = Column(JSONB, nullable=False)
    
    # Indexed fields to easily locate and release reserved inventory during cleanup
    target_type = Column(String, nullable=False) # 'package' or 'room'
    variant_id = Column(Integer, nullable=True) 
    room_variant_id = Column(Integer, nullable=True) 
    travel_date = Column(Date, nullable=False)
    quantity = Column(Integer, nullable=False)
    
    amount_payable = Column(Numeric(12, 2), nullable=False)
    coupon_applied = Column(String(50), nullable=True)
    
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)

class EmailLog(BaseModel):
    __tablename__ = "email_logs"

    booking_id = Column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    recipient_email = Column(String, nullable=True)
    email_type = Column(String, nullable=False) # e.g. "PARTIAL_PAYMENT", "FULL_PAYMENT"
    delivery_status = Column(String, nullable=False) # e.g. "SENT", "FAILED", "SKIPPED"
    failure_reason = Column(String, nullable=True)
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    booking = relationship("Booking")

