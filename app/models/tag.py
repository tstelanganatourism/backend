from sqlalchemy import Column, String, Integer, Boolean
from app.models.base import BaseModel

class Tag(BaseModel):
    __tablename__ = "tags"

    name = Column(String, unique=True, nullable=False, index=True)
    priority = Column(Integer, default=0)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False)
