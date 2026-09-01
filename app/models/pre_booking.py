"""
PreBooking model — stores early pre-booking leads from the /prebooking page.
These are NOT full bookings; they are interest registrations that require
manual confirmation by the admin team.
"""
from sqlalchemy import Column, String, Integer, Date, Boolean, Index
from app.models.base import BaseModel


class PreBooking(BaseModel):
    __tablename__ = "pre_bookings"

    # Public reference ID shown to customer
    ref_id = Column(String(30), unique=True, nullable=False, index=True)

    # Package details (denormalized for simplicity — no FK needed)
    package_id = Column(String(100), nullable=False, index=True)     # slug or id string
    package_name = Column(String(255), nullable=False)

    # Travel intent
    travel_date = Column(Date, nullable=False, index=True)
    adult_count = Column(Integer, default=1, nullable=False)
    child_count = Column(Integer, default=0, nullable=False)

    # Customer contact
    customer_name = Column(String(255), nullable=False)
    customer_email = Column(String(255), nullable=False, index=True)
    customer_phone = Column(String(20), nullable=False)
    notes = Column(String(1000), nullable=True)

    # Admin workflow
    is_confirmed = Column(Boolean, default=False, server_default="false", nullable=False)
    is_contacted = Column(Boolean, default=False, server_default="false", nullable=False)
    admin_notes = Column(String(1000), nullable=True)

    # Email tracking
    user_email_sent = Column(Boolean, default=False, server_default="false", nullable=False)
    admin_email_sent = Column(Boolean, default=False, server_default="false", nullable=False)

    __table_args__ = (
        Index("ix_pre_bookings_created_at", "created_at"),
        Index("ix_pre_bookings_travel_date", "travel_date"),
    )
