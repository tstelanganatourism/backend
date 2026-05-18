from sqlalchemy import Column, String, Enum as SQLEnum, DateTime, Boolean, Numeric, Text
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import UserRole, AccountStatus

class User(BaseModel):
    __tablename__ = "users"

    role = Column(SQLEnum(UserRole), default=UserRole.USER, server_default="USER", nullable=False)
    full_name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=True)
    phone_number = Column(String, unique=True, index=True, nullable=True)
    password_hash = Column(String, nullable=True)
    google_id = Column(String, unique=True, index=True, nullable=True)
    account_status = Column(SQLEnum(AccountStatus), default=AccountStatus.ACTIVE, server_default="ACTIVE", nullable=False)
    last_login = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False)

    # Agent-specific fields (nullable — only populated for role=AGENT)
    commission_percentage = Column(Numeric(5, 2), default=0.00, server_default="0.00", nullable=True)
    company_name = Column(String, nullable=True)
    gst_number = Column(String, nullable=True)
    address = Column(Text, nullable=True)
    admin_notes = Column(Text, nullable=True)
    
    # Profile picture
    avatar_url = Column(String, nullable=True)

    # Relationships
    agent_bookings = relationship("Booking", foreign_keys="Booking.agent_id", back_populates="agent", lazy="dynamic")

