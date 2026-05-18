from sqlalchemy import Column, String, Numeric, Integer, Time, Boolean, ForeignKey, UniqueConstraint, Computed, Date, CheckConstraint, Enum as SQLEnum, Index, Table, BigInteger
from sqlalchemy.orm import relationship
from app.models.base import BaseModel, SortableMixin, SEOMixin
from app.models.enums import PolicyType, PublishStatus, DocumentGenerationStatus
from sqlalchemy.dialects.postgresql import JSONB

# Association table for Room -> Tags
room_tags = Table(
    "room_tags",
    BaseModel.metadata,
    Column("room_id", BigInteger, ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
)

class Room(BaseModel, SEOMixin):
    """
    Represents the Lodge or Property.
    """
    __tablename__ = "rooms"

    lodge_name = Column(String, nullable=False, index=True)
    slug = Column(String, unique=True, nullable=False, index=True)
    description = Column(String, nullable=True)
    address = Column(String, nullable=True)
    map_url = Column(String, nullable=True)
    facilities = Column(JSONB, nullable=True) # Array of strings
    cover_image_url = Column(String, nullable=True)
    brochure_pdf_url = Column(String, nullable=True)
    generated_brochure_url = Column(String, nullable=True)
    brochure_generation_status = Column(SQLEnum(DocumentGenerationStatus), default=DocumentGenerationStatus.MISSING, server_default="MISSING", nullable=False)
    
    total_rooms = Column(Integer, nullable=False)
    
    # We will compute booked_rooms logically or manage via triggers. For now, keep it for legacy compat, but it's fundamentally flawed if booked at variant level.
    # Actually, as per user: "Future inventory logic must evolve toward room_variant_inventory instead of shared room inventory."
    # We'll leave total_rooms for now as a capacity upper bound for the lodge.
    booked_rooms = Column(Integer, default=0, server_default="0", nullable=False)
    available_rooms = Column(Integer, Computed('total_rooms - booked_rooms', persisted=True))

    slot_start = Column(Time, nullable=False)
    slot_end = Column(Time, nullable=False)
    booking_slots = Column(JSONB, nullable=True) # Array of objects with title, slot_start, slot_end

    order_priority = Column(Integer, default=0, server_default="0", nullable=False, index=True)
    is_featured = Column(Boolean, default=False, server_default="false", nullable=False, index=True)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False, index=True)
    status = Column(SQLEnum(PublishStatus), default=PublishStatus.DRAFT, server_default="DRAFT", nullable=False, index=True)

    # Relationships
    tags = relationship("Tag", secondary=room_tags)
    variants = relationship("RoomVariant", back_populates="room", cascade="all, delete-orphan")
    inventory = relationship("RoomSlotInventory", back_populates="room")
    gallery = relationship("RoomGalleryImage", back_populates="room", cascade="all, delete-orphan", order_by="RoomGalleryImage.sort_order")
    highlights = relationship("RoomHighlight", back_populates="room", cascade="all, delete-orphan", order_by="RoomHighlight.sort_order")
    faqs = relationship("RoomFAQ", back_populates="room", cascade="all, delete-orphan", order_by="RoomFAQ.sort_order")
    policies = relationship("RoomPolicy", back_populates="room", cascade="all, delete-orphan", order_by="RoomPolicy.sort_order")

    __table_args__ = (
        Index('ix_rooms_facilities_gin', 'facilities', postgresql_using='gin'),
        Index("ix_rooms_public_priority", "is_active", "deleted_at", "order_priority", "id"),
        Index("ix_rooms_public_featured", "is_featured", "is_active", "deleted_at", "order_priority", "id"),
        Index("ix_rooms_admin_listing", "deleted_at", "status", "order_priority", "created_at"),
    )

class RoomGalleryImage(BaseModel, SortableMixin):
    __tablename__ = "room_gallery_images"
    room_id = Column(ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True)
    image_url = Column(String, nullable=False)
    alt_text = Column(String, nullable=True)
    is_cover = Column(Boolean, default=False, server_default="false")
    room = relationship("Room", back_populates="gallery")

class RoomHighlight(BaseModel, SortableMixin):
    __tablename__ = "room_highlights"
    room_id = Column(ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    icon = Column(String, nullable=True)
    room = relationship("Room", back_populates="highlights")

class RoomFAQ(BaseModel, SortableMixin):
    __tablename__ = "room_faqs"
    room_id = Column(ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True)
    question = Column(String, nullable=False)
    answer = Column(String, nullable=False)
    room = relationship("Room", back_populates="faqs")

class RoomPolicy(BaseModel, SortableMixin):
    __tablename__ = "room_policies"
    room_id = Column(ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(SQLEnum(PolicyType), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    room = relationship("Room", back_populates="policies")

class RoomVariant(BaseModel):
    """
    Represents specific room types within a lodge (e.g., A/C, Non-A/C)
    """
    __tablename__ = "room_variants"

    room_id = Column(ForeignKey("rooms.id"), nullable=False, index=True)
    variant_name = Column(String, nullable=False, index=True)
    
    weekday_price = Column(Numeric(10, 2), nullable=False, index=True)
    weekend_price = Column(Numeric(10, 2), nullable=False, index=True)
    
    capacity_per_room = Column(Integer, nullable=False)
    
    is_active = Column(Boolean, default=True, server_default="true", nullable=False, index=True)

    __table_args__ = (
        Index("ix_room_variants_public_price", "room_id", "is_active", "deleted_at", "weekday_price"),
        UniqueConstraint('room_id', 'variant_name', name='uq_room_variant'),
    )

    # Relationships
    room = relationship("Room", back_populates="variants")

class RoomSlotInventory(BaseModel):
    __tablename__ = "room_slot_inventory"

    room_id = Column(ForeignKey("rooms.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True) 
    slot_start = Column(Time, nullable=False)
    slot_end = Column(Time, nullable=False)
    total_rooms = Column(Integer, nullable=False)
    booked_rooms = Column(Integer, default=0, server_default="0", nullable=False)
    
    # In PostgreSQL we can use Computed. In SQLAlchemy we define it like this:
    available_rooms = Column(Integer, Computed('total_rooms - booked_rooms', persisted=True))

    __table_args__ = (
        UniqueConstraint('room_id', 'date', 'slot_start', 'slot_end', name='uq_room_slot_inventory'),
        CheckConstraint("booked_rooms <= total_rooms", name="chk_room_capacity"),
    )

    room = relationship("Room", back_populates="inventory")
