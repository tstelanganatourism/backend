from sqlalchemy import Column, String, Numeric, Integer, Boolean, ForeignKey, UniqueConstraint, Table, Enum as SQLEnum, Date, CheckConstraint, BigInteger, Index
from sqlalchemy.orm import relationship
from app.models.base import BaseModel, SortableMixin, SEOMixin
from app.models.enums import PackageType, RegionType, PolicyType, PublishStatus, DocumentGenerationStatus

# Association table for Package -> Tags
package_tags = Table(
    'package_tags',
    BaseModel.metadata,
    Column('package_id', BigInteger, ForeignKey('packages.id', ondelete='CASCADE'), primary_key=True),
    Column('tag_id', BigInteger, ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True)
)

class Package(BaseModel, SEOMixin):
    __tablename__ = "packages"

    type = Column(SQLEnum(PackageType), nullable=False)
    title = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    description = Column(String, nullable=True)
    cover_image_url = Column(String, nullable=True)
    brochure_pdf_url = Column(String, nullable=True)
    generated_brochure_url = Column(String, nullable=True)
    brochure_generation_status = Column(SQLEnum(DocumentGenerationStatus), default=DocumentGenerationStatus.MISSING, server_default="MISSING", nullable=False)
    region = Column(SQLEnum(RegionType), nullable=True)
    order_priority = Column(Integer, default=0)
    is_featured = Column(Boolean, default=False, server_default="false", nullable=False)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False, index=True)
    status = Column(SQLEnum(PublishStatus), default=PublishStatus.DRAFT, server_default="DRAFT", nullable=False, index=True)

    tags = relationship("Tag", secondary=package_tags)
    variants = relationship("PackageVariant", back_populates="package", cascade="all, delete-orphan")
    gallery = relationship("PackageGalleryImage", back_populates="package", cascade="all, delete-orphan", order_by="PackageGalleryImage.sort_order")
    itinerary = relationship("PackageItineraryDay", back_populates="package", cascade="all, delete-orphan", order_by="PackageItineraryDay.sort_order")
    highlights = relationship("PackageHighlight", back_populates="package", cascade="all, delete-orphan", order_by="PackageHighlight.sort_order")
    inclusions = relationship("PackageInclusion", back_populates="package", cascade="all, delete-orphan", order_by="PackageInclusion.sort_order")
    exclusions = relationship("PackageExclusion", back_populates="package", cascade="all, delete-orphan", order_by="PackageExclusion.sort_order")
    boarding_points = relationship("PackageBoardingPoint", back_populates="package", cascade="all, delete-orphan", order_by="PackageBoardingPoint.sort_order")
    faqs = relationship("PackageFAQ", back_populates="package", cascade="all, delete-orphan", order_by="PackageFAQ.sort_order")
    policies = relationship("PackagePolicy", back_populates="package", cascade="all, delete-orphan", order_by="PackagePolicy.sort_order")

    __table_args__ = (
        Index("ix_packages_public_priority", "is_active", "deleted_at", "order_priority", "id"),
        Index("ix_packages_public_featured", "is_featured", "is_active", "deleted_at", "order_priority", "id"),
        Index("ix_packages_admin_listing", "deleted_at", "status", "order_priority", "created_at"),
    )

class PackageGalleryImage(BaseModel, SortableMixin):
    __tablename__ = "package_gallery_images"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    image_url = Column(String, nullable=False)
    alt_text = Column(String, nullable=True)
    is_cover = Column(Boolean, default=False, server_default="false")
    category = Column(String, nullable=True)  # e.g. 'boat', 'stay', 'food', 'scenery'
    package = relationship("Package", back_populates="gallery")

class PackageItineraryDay(BaseModel, SortableMixin):
    __tablename__ = "package_itinerary_days"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    day_number = Column(Integer, nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    icon = Column(String, nullable=True)
    timing = Column(String, nullable=True)          # e.g. '10:30 AM'
    duration_at_stop = Column(String, nullable=True) # e.g. '45 Mins'
    image_url = Column(String, nullable=True)        # stop-specific photo
    meal_included = Column(Boolean, default=False, server_default="false", nullable=False)
    package = relationship("Package", back_populates="itinerary")

class PackageHighlight(BaseModel, SortableMixin):
    __tablename__ = "package_highlights"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    icon = Column(String, nullable=True)
    package = relationship("Package", back_populates="highlights")

class PackageInclusion(BaseModel, SortableMixin):
    __tablename__ = "package_inclusions"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String, nullable=False)
    icon = Column(String, nullable=True)
    package = relationship("Package", back_populates="inclusions")

class PackageExclusion(BaseModel, SortableMixin):
    __tablename__ = "package_exclusions"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String, nullable=False)
    icon = Column(String, nullable=True)
    package = relationship("Package", back_populates="exclusions")

class PackageBoardingPoint(BaseModel, SortableMixin):
    __tablename__ = "package_boarding_points"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    address = Column(String, nullable=True)
    map_url = Column(String, nullable=True)
    departure_time = Column(String, nullable=True)
    landmark = Column(String, nullable=True)             # e.g. 'Near SBI ATM, Bhadrachalam'
    contact_number = Column(String, nullable=True)       # local guide/operator number
    pickup_instructions = Column(String, nullable=True)  # specific directions for tourists
    return_drop_info = Column(String, nullable=True)     # return timing/location
    package = relationship("Package", back_populates="boarding_points")

class PackageFAQ(BaseModel, SortableMixin):
    __tablename__ = "package_faqs"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    question = Column(String, nullable=False)
    answer = Column(String, nullable=False)
    package = relationship("Package", back_populates="faqs")

class PackagePolicy(BaseModel, SortableMixin):
    __tablename__ = "package_policies"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(SQLEnum(PolicyType), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    package = relationship("Package", back_populates="policies")

class PackageVariant(BaseModel):
    __tablename__ = "package_variants"

    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    adult_price = Column(Numeric(10, 2), nullable=False)
    child_price = Column(Numeric(10, 2), nullable=False)
    transport_info = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False, index=True)

    package = relationship("Package", back_populates="variants")
    inventory = relationship("PackageVariantInventory", back_populates="variant")

    __table_args__ = (
        Index("ix_package_variants_public_price", "package_id", "is_active", "deleted_at", "adult_price"),
    )

class PackageVariantInventory(BaseModel):
    __tablename__ = "package_variant_inventory"

    variant_id = Column(ForeignKey("package_variants.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    total_capacity = Column(Integer, nullable=False, default=500, server_default="500")
    booked_count = Column(Integer, default=0, server_default="0", nullable=False)
    # Admin can manually close a date regardless of remaining seats
    is_closed = Column(Boolean, default=False, server_default="false", nullable=False)
    # Optional per-date price override (overrides variant base price for that date)
    price_override = Column(Numeric(10, 2), nullable=True)

    __table_args__ = (
        UniqueConstraint('variant_id', 'date', name='uq_variant_inventory'),
        CheckConstraint("booked_count <= total_capacity", name="chk_variant_capacity"),
    )

    variant = relationship("PackageVariant", back_populates="inventory")
