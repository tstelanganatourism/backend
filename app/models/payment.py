from sqlalchemy import Column, String, Numeric, ForeignKey, Enum as SQLEnum, BigInteger
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import PaymentStatus


class Payment(BaseModel):
    """
    Immutable payment ledger. Every payment attempt — online or manual — is a
    separate row. Booking status is always derived by summing CAPTURED rows.
    """
    __tablename__ = "payments"

    booking_id = Column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)

    # ─── Unified Idempotency Key ──────────────────────────────────────────────
    # For Razorpay: set to razorpay_order_id
    # For Admin cash: set to "CASH_{public_id}_{uuid[:8].upper()}"
    # This is the single deduplication key regardless of payment source.
    payment_reference_id = Column(String, unique=True, nullable=False, index=True)

    # ─── Razorpay-Specific Fields (nullable for cash/offline payments) ────────
    razorpay_order_id = Column(String, unique=True, index=True, nullable=True)
    razorpay_payment_id = Column(String, unique=True, index=True, nullable=True)
    razorpay_signature = Column(String(255), nullable=True)

    # ─── Core Ledger Fields ───────────────────────────────────────────────────
    amount = Column(Numeric(12, 2), nullable=False)
    status = Column(SQLEnum(PaymentStatus), default=PaymentStatus.CREATED, server_default="CREATED", nullable=False, index=True)
    # RAZORPAY | CASH | BANK_TRANSFER
    payment_method = Column(String(50), nullable=False, server_default="RAZORPAY")

    # ─── Structured Audit Columns ─────────────────────────────────────────────
    # 'RAZORPAY' or 'ADMIN' — used for business logic / reporting
    collected_by_type = Column(String(50), nullable=False, server_default="RAZORPAY")
    # FK to admin user who entered a manual payment (null for online payments)
    collected_by_user_id = Column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Human-readable display label only — never used for business logic
    collected_by_label = Column(String(255), nullable=True)

    # ─── Error Tracking ───────────────────────────────────────────────────────
    error_code = Column(String(100), nullable=True)
    error_description = Column(String, nullable=True)

    booking = relationship("Booking", back_populates="payments")
