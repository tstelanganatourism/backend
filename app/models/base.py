from sqlalchemy import Column, DateTime, BigInteger, func, String
from datetime import datetime
from app.db.base import Base

class BaseModel(Base):
    __abstract__ = True
    
    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

class SortableMixin:
    sort_order = Column(BigInteger, default=0, server_default="0", nullable=False)

class SEOMixin:
    meta_title = Column(String, nullable=True)
    meta_description = Column(String, nullable=True)
    og_image_url = Column(String, nullable=True)
    canonical_url = Column(String, nullable=True)
