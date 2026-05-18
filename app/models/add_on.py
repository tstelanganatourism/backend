from sqlalchemy import Column, String, Numeric, Boolean
from app.models.base import BaseModel

class AddOn(BaseModel):
    __tablename__ = "add_ons"

    title = Column(String, nullable=False)
    price_per_person = Column(Numeric(10, 2), nullable=False)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False)
