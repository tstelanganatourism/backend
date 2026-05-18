from sqlalchemy import Column, String, JSON, Integer, ForeignKey, BigInteger
from sqlalchemy.orm import relationship
from app.models.base import BaseModel

class SystemSettings(BaseModel):
    __tablename__ = "system_settings"

    # Contact & Company
    company_name = Column(String, nullable=True)
    support_email = Column(String, nullable=True)
    whatsapp_number = Column(String, nullable=True)
    address = Column(String, nullable=True)
    gst_number = Column(String, nullable=True)
    
    # Financials
    global_tax_percentage = Column(Integer, default=0, server_default="0") # e.g., 5 for 5%
    razorpay_key_id = Column(String, nullable=True) # Public key can be stored here if dynamic, otherwise .env
    
    # Policies & Content (Stored as JSON or Text, rich text)
    booking_rules = Column(String, nullable=True)
    cancellation_policies = Column(String, nullable=True)
    
    # Social Links
    social_links = Column(JSON, nullable=True, default={}) # e.g., {"facebook": "url", "instagram": "url"}
    
    # SEO Defaults
    default_meta_title = Column(String, nullable=True)
    default_meta_description = Column(String, nullable=True)
    
    # Extensibility
    extra_config = Column(JSON, nullable=True, default={})

class AuditLog(BaseModel):
    __tablename__ = "audit_logs"

    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = Column(String, nullable=False, index=True) # e.g., "CREATE", "UPDATE", "DELETE", "LOGIN"
    entity_type = Column(String, nullable=True, index=True) # e.g., "Package", "Room", "Booking"
    entity_id = Column(String, nullable=True, index=True) # Can be string to support UUIDs or composite keys
    
    details = Column(JSON, nullable=True) # To store {"old_value": ..., "new_value": ...} or specific context
    ip_address = Column(String, nullable=True)
    
    user = relationship("User")
