from sqlalchemy import Column, String, Numeric, Integer, Boolean, ForeignKey, UniqueConstraint, Table, Enum as SQLEnum, Date, CheckConstraint, BigInteger, Index, text
from sqlalchemy.orm import relationship
from app.models.base import BaseModel, SortableMixin, SEOMixin
from app.models.enums import PackageType, RegionType, PolicyType, PublishStatus, DocumentGenerationStatus, TransportOptionType, AdvancePaymentType, MealType

# Association table for Package -> Tags
package_tags = Table(
    'package_tags',
    BaseModel.metadata,
    Column('package_id', BigInteger, ForeignKey('packages.id', ondelete='CASCADE'), primary_key=True),
    Column('tag_id', BigInteger, ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True)
)

# Association table for PackageCategory -> Package (many-to-many)
package_category_assignments = Table(
    'package_category_assignments',
    BaseModel.metadata,
    Column('category_id', BigInteger, ForeignKey('package_categories.id', ondelete='CASCADE'), primary_key=True),
    Column('package_id', BigInteger, ForeignKey('packages.id', ondelete='CASCADE'), primary_key=True)
)

class PackageCategory(BaseModel, SortableMixin):
    """
    A top-level grouping for packages (e.g. 'Papikondalu Packages', 'Bhadrachalam Packages').
    Users first see categories on the /packages page, then drill into a category to see packages.
    """
    __tablename__ = "package_categories"

    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    description = Column(String, nullable=True)
    cover_image_url = Column(String, nullable=True)
    icon = Column(String, nullable=True)  # optional emoji or icon name
    is_active = Column(Boolean, default=True, server_default='true', nullable=False, index=True)

    # Many-to-many: packages in this category
    packages = relationship(
        'Package',
        secondary=package_category_assignments,
        back_populates='categories',
        lazy='selectin',
    )

class Package(BaseModel, SEOMixin):
    __tablename__ = "packages"

    type = Column(SQLEnum(PackageType), nullable=False)
    title = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    description = Column(String, nullable=True)
    duration = Column(String, nullable=True)
    place = Column(String, nullable=True)
    cover_image_url = Column(String, nullable=True)
    brochure_pdf_url = Column(String, nullable=True)
    generated_brochure_url = Column(String, nullable=True)
    brochure_generation_status = Column(SQLEnum(DocumentGenerationStatus), default=DocumentGenerationStatus.MISSING, server_default="MISSING", nullable=False)
    region = Column(SQLEnum(RegionType), nullable=True)
    order_priority = Column(Integer, default=0)
    starting_price = Column(Numeric(12, 2), nullable=False, server_default="0.00", index=True)
    has_transport = Column(Boolean, default=False, server_default="false", nullable=False)
    has_refreshments = Column(Boolean, default=False, server_default="false", nullable=False)
    refreshment_adult_price = Column(Numeric(10, 2), nullable=True)
    refreshment_child_price = Column(Numeric(10, 2), nullable=True)
    # Student Package support
    is_student_package = Column(Boolean, default=False, server_default="false", nullable=False)
    refreshment_student_price = Column(Numeric(10, 2), nullable=True)  # per-student refreshment cost
    refreshments_min_passengers = Column(Integer, default=1, server_default="1", nullable=False)

    # Food / Meals Option fields (distinct from refreshments)
    has_food_option = Column(Boolean, default=False, server_default="false", nullable=False)
    food_adult_price = Column(Numeric(10, 2), nullable=True)
    food_child_price = Column(Numeric(10, 2), nullable=True)
    food_student_price = Column(Numeric(10, 2), nullable=True)

    is_featured = Column(Boolean, default=False, server_default="false", nullable=False)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False, index=True)
    
    advance_payment_type = Column(SQLEnum(AdvancePaymentType), default=AdvancePaymentType.FULL_PAYMENT, server_default="FULL_PAYMENT", nullable=False)
    advance_payment_value = Column(Numeric(12, 2), default=0.00, server_default="0.00", nullable=False)
    status = Column(SQLEnum(PublishStatus), default=PublishStatus.DRAFT, server_default="DRAFT", nullable=False, index=True)
    min_passengers = Column(Integer, default=1, server_default="1", nullable=False)

    tags = relationship("Tag", secondary=package_tags)
    categories = relationship("PackageCategory", secondary=package_category_assignments, back_populates="packages")
    variants = relationship("PackageVariant", back_populates="package", cascade="all, delete-orphan")
    transport_options = relationship("PackageTransportOption", back_populates="package", cascade="all, delete-orphan", order_by="PackageTransportOption.id")
    gallery = relationship("PackageGalleryImage", back_populates="package", cascade="all, delete-orphan", order_by="PackageGalleryImage.sort_order")
    itinerary = relationship("PackageItineraryDay", back_populates="package", cascade="all, delete-orphan", order_by="PackageItineraryDay.sort_order")
    highlights = relationship("PackageHighlight", back_populates="package", cascade="all, delete-orphan", order_by="PackageHighlight.sort_order")
    inclusions = relationship("PackageInclusion", back_populates="package", cascade="all, delete-orphan", order_by="PackageInclusion.sort_order")
    exclusions = relationship("PackageExclusion", back_populates="package", cascade="all, delete-orphan", order_by="PackageExclusion.sort_order")
    boarding_points = relationship("PackageBoardingPoint", back_populates="package", cascade="all, delete-orphan", order_by="PackageBoardingPoint.sort_order")
    faqs = relationship("PackageFAQ", back_populates="package", cascade="all, delete-orphan", order_by="PackageFAQ.sort_order")
    policies = relationship("PackagePolicy", back_populates="package", cascade="all, delete-orphan", order_by="PackagePolicy.sort_order")
    meals = relationship("PackageMealItem", back_populates="package", cascade="all, delete-orphan", order_by="PackageMealItem.sort_order")
    extras = relationship("PackageExtra", back_populates="package", cascade="all, delete-orphan", order_by="PackageExtra.sort_order")

    __table_args__ = (
        Index("ix_packages_public_priority", "is_active", "deleted_at", "order_priority", "id"),
        Index("ix_packages_public_featured", "is_featured", "is_active", "deleted_at", "order_priority", "id"),
        Index("ix_packages_admin_listing", "deleted_at", "status", "order_priority", "created_at"),
        # Partial exact filters for storefront
        Index("ix_packages_type_region", "type", "region", "is_active", "order_priority", postgresql_where=text("deleted_at IS NULL")),
        Index("ix_packages_fts", text("to_tsvector('english'::regconfig, title || ' ' || coalesce(description, ''))"), postgresql_using='gin'),
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

class PackageMealItem(BaseModel, SortableMixin):
    """Represents a single meal served during a package (e.g. Day-1 Lunch)."""
    __tablename__ = "package_meal_items"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    meal_type = Column(SQLEnum(MealType), nullable=False)         # BREAKFAST / LUNCH / DINNER / SNACKS
    name = Column(String, nullable=False)                         # e.g. "Veg Biryani + Raita"
    serving_time = Column(String, nullable=True)                  # e.g. "1:00 PM"
    description = Column(String, nullable=True)                   # optional details
    cost_per_person = Column(Numeric(10, 2), nullable=False, server_default="0.00")
    is_vegetarian = Column(Boolean, default=True, server_default="true", nullable=False)
    day_number = Column(Integer, nullable=True)                   # which day (for multi-day packages)
    package = relationship("Package", back_populates="meals")

class PackageExtra(BaseModel, SortableMixin):
    """Represents a customizable add-on / extra for a package (e.g. Rajahmundry Drop, Special Transport, Guide)."""
    __tablename__ = "package_extras"
    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)                         # e.g. "Rajahmundry Dropping Extra"
    description = Column(String, nullable=True)                   # e.g. "Reaches Pattiseema revu, By road journey to Rajahmundry"
    adult_price = Column(Numeric(10, 2), nullable=True)
    child_price = Column(Numeric(10, 2), nullable=True)
    student_price = Column(Numeric(10, 2), nullable=True)
    min_passengers = Column(Integer, default=1, server_default="1", nullable=False)
    package = relationship("Package", back_populates="extras")

class PackageVariant(BaseModel):
    __tablename__ = "package_variants"

    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    adult_price = Column(Numeric(10, 2), nullable=False)
    child_price = Column(Numeric(10, 2), nullable=False)
    weekend_adult_price = Column(Numeric(10, 2), nullable=True)
    weekend_child_price = Column(Numeric(10, 2), nullable=True)
    # Student Package pricing
    student_price = Column(Numeric(10, 2), nullable=True)  # per-student price (used when package.is_student_package=True)
    weekend_student_price = Column(Numeric(10, 2), nullable=True)
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
    reserved_count = Column(Integer, default=0, server_default="0", nullable=False)
    # Admin can manually close a date regardless of remaining seats
    is_closed = Column(Boolean, default=False, server_default="false", nullable=False)
    # Optional per-date price override (overrides variant base price for that date)
    price_override = Column(Numeric(10, 2), nullable=True)

    __table_args__ = (
        UniqueConstraint('variant_id', 'date', name='uq_variant_inventory'),
        CheckConstraint("booked_count + reserved_count <= total_capacity", name="chk_variant_capacity"),
    )

    variant = relationship("PackageVariant", back_populates="inventory")

class PackageTransportOption(BaseModel):
    __tablename__ = "package_transport_options"

    package_id = Column(ForeignKey("packages.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    type = Column(SQLEnum(TransportOptionType), nullable=False)
    capacity = Column(Integer, nullable=False, default=1, server_default="1")
    
    # Pricing for SHARED
    adult_price = Column(Numeric(10, 2), nullable=True)
    child_price = Column(Numeric(10, 2), nullable=True)
    weekend_adult_price = Column(Numeric(10, 2), nullable=True)
    weekend_child_price = Column(Numeric(10, 2), nullable=True)
    # Student pricing for SHARED transport
    student_price = Column(Numeric(10, 2), nullable=True)
    weekend_student_price = Column(Numeric(10, 2), nullable=True)
    
    # Pricing for SEPARATE_VEHICLE
    fixed_price = Column(Numeric(10, 2), nullable=True)
    weekend_fixed_price = Column(Numeric(10, 2), nullable=True)

    package = relationship("Package", back_populates="transport_options")
    inventory = relationship(
        "PackageTransportInventory",
        back_populates="transport_option",
        cascade="all, delete-orphan",
    )


class PackageTransportInventory(BaseModel):
    """
    Per-date inventory for each PackageTransportOption.
    SEPARATE_VEHICLE: available_count = number of vehicles admin has for this date.
    SHARED:           available_count = number of seats admin opens for this date.
    booked_count is incremented on each confirmed booking.
    No row for a date = transport option NOT available for that date.
    """
    __tablename__ = "package_transport_inventory"

    transport_option_id = Column(
        ForeignKey("package_transport_options.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    date = Column(Date, nullable=False, index=True)

    # SEPARATE_VEHICLE: vehicles available | SHARED: total seats available
    available_count = Column(Integer, nullable=False, default=1, server_default="1")
    # Auto-incremented when bookings are confirmed
    booked_count = Column(Integer, nullable=False, default=0, server_default="0")
    # Admin can manually block this transport option for the day
    is_closed = Column(Boolean, nullable=False, default=False, server_default="false")
    # Optional per-date price override
    price_override = Column(Numeric(10, 2), nullable=True)

    transport_option = relationship("PackageTransportOption", back_populates="inventory")

    __table_args__ = (
        UniqueConstraint("transport_option_id", "date", name="uq_transport_inventory"),
    )

    @property
    def remaining(self) -> int:
        return max(0, self.available_count - self.booked_count)
