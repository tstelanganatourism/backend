from typing import List, Optional, TypeVar, Generic
from pydantic import Field
from decimal import Decimal
from datetime import time
from app.schemas.base import AppBaseModel
from app.models.enums import PackageType, RegionType, PolicyType

T = TypeVar("T")

class PaginatedResponse(AppBaseModel, Generic[T]):
    items: List[T]
    total: int
    page: int
    size: int
    has_next: bool
    has_prev: bool

class SEOSchema(AppBaseModel):
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    og_image_url: Optional[str] = None
    canonical_url: Optional[str] = None

class TagDTO(AppBaseModel):
    name: str

class GalleryImageDTO(AppBaseModel):
    id: int
    image_url: str
    alt_text: Optional[str] = None
    is_cover: bool
    category: Optional[str] = None
    sort_order: int

class FAQDTO(AppBaseModel):
    id: int
    question: str
    answer: str
    sort_order: int

class PolicyDTO(AppBaseModel):
    id: int
    type: PolicyType
    title: str
    description: str
    sort_order: int

class HighlightDTO(AppBaseModel):
    id: int
    title: str
    icon: Optional[str] = None
    sort_order: int

class InclusionDTO(AppBaseModel):
    id: int
    label: str
    icon: Optional[str] = None
    sort_order: int

class PackageVariantPublicDTO(AppBaseModel):
    id: int
    title: str
    adult_price: Decimal
    child_price: Decimal
    weekend_adult_price: Optional[Decimal] = None
    weekend_child_price: Optional[Decimal] = None
    student_price: Optional[Decimal] = None
    weekend_student_price: Optional[Decimal] = None
    transport_info: Optional[str] = None

class PackageListDTO(AppBaseModel):
    id: int
    slug: str
    title: str
    type: PackageType
    duration: Optional[str] = None
    place: Optional[str] = None
    region: Optional[RegionType] = None
    brochure_pdf_url: Optional[str] = None
    generated_brochure_url: Optional[str] = None
    cover_image_url: Optional[str] = None
    is_active: bool = True
    is_featured: bool
    is_student_package: bool = False
    tags: List[str] = Field(default_factory=list)
    starting_price: Optional[Decimal] = None
    transport_info: Optional[str] = None
    min_passengers: int = 1
    variants: List[PackageVariantPublicDTO] = Field(default_factory=list)

class PackageItineraryDayDTO(AppBaseModel):
    id: int
    day_number: int
    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    timing: Optional[str] = None
    duration_at_stop: Optional[str] = None
    image_url: Optional[str] = None
    meal_included: bool = False
    sort_order: int

class PackageBoardingPointDTO(AppBaseModel):
    id: int
    title: str
    address: Optional[str] = None
    map_url: Optional[str] = None
    departure_time: Optional[str] = None
    landmark: Optional[str] = None
    contact_number: Optional[str] = None
    pickup_instructions: Optional[str] = None
    return_drop_info: Optional[str] = None
    sort_order: int

class TransportOptionPublicDTO(AppBaseModel):
    id: int
    type: str  # 'SHARED' | 'SEPARATE_VEHICLE'
    title: str
    capacity: Optional[int] = None
    adult_price: Optional[Decimal] = None
    child_price: Optional[Decimal] = None
    weekend_adult_price: Optional[Decimal] = None
    weekend_child_price: Optional[Decimal] = None
    student_price: Optional[Decimal] = None
    weekend_student_price: Optional[Decimal] = None
    fixed_price: Optional[Decimal] = None
    weekend_fixed_price: Optional[Decimal] = None

class PackageMealItemPublicDTO(AppBaseModel):
    id: int
    meal_type: str
    name: str
    serving_time: Optional[str] = None
    description: Optional[str] = None
    cost_per_person: Decimal = Decimal("0.00")
    is_vegetarian: bool = True
    day_number: Optional[int] = None
    sort_order: int

class PackageExtraPublicDTO(AppBaseModel):
    id: int
    title: str
    description: Optional[str] = None
    adult_price: Optional[Decimal] = None
    child_price: Optional[Decimal] = None
    student_price: Optional[Decimal] = None
    min_passengers: int = 1
    sort_order: int

class PackageDetailDTO(PackageListDTO, SEOSchema):
    description: Optional[str] = None
    brochure_pdf_url: Optional[str] = None
    generated_brochure_url: Optional[str] = None
    has_transport: bool = False
    transport_options: List[TransportOptionPublicDTO] = Field(default_factory=list)
    has_refreshments: bool = False
    refreshment_adult_price: Optional[Decimal] = None
    refreshment_child_price: Optional[Decimal] = None
    refreshment_student_price: Optional[Decimal] = None
    refreshments_min_passengers: int = 1
    has_food_option: bool = False
    food_adult_price: Optional[Decimal] = None
    food_child_price: Optional[Decimal] = None
    food_student_price: Optional[Decimal] = None
    variants: List[PackageVariantPublicDTO] = Field(default_factory=list)
    gallery: List[GalleryImageDTO] = Field(default_factory=list)
    itinerary: List[PackageItineraryDayDTO] = Field(default_factory=list)
    highlights: List[HighlightDTO] = Field(default_factory=list)
    inclusions: List[InclusionDTO] = Field(default_factory=list)
    exclusions: List[InclusionDTO] = Field(default_factory=list)
    boarding_points: List[PackageBoardingPointDTO] = Field(default_factory=list)
    faqs: List[FAQDTO] = Field(default_factory=list)
    policies: List[PolicyDTO] = Field(default_factory=list)
    meals: List[PackageMealItemPublicDTO] = Field(default_factory=list)
    extras: List[PackageExtraPublicDTO] = Field(default_factory=list)

class RoomVariantPublicDTO(AppBaseModel):
    id: int
    variant_name: str
    weekday_price: Decimal
    weekend_price: Decimal
    capacity_per_room: int

class RoomBookingSlotDTO(AppBaseModel):
    title: str
    slot_start: str
    slot_end: str

class RoomListDTO(AppBaseModel):
    id: int
    slug: str
    lodge_name: str
    cover_image_url: Optional[str] = None
    is_featured: bool
    starting_price: Optional[Decimal] = None
    starting_weekend_price: Optional[Decimal] = None
    address: Optional[str] = None
    map_url: Optional[str] = None
    facilities: List[str] = Field(default_factory=list)

class RoomDetailDTO(RoomListDTO, SEOSchema):
    description: Optional[str] = None
    brochure_pdf_url: Optional[str] = None
    generated_brochure_url: Optional[str] = None
    total_rooms: Optional[int] = None
    slot_start: Optional[time] = None
    slot_end: Optional[time] = None
    booking_slots: List[RoomBookingSlotDTO] = Field(default_factory=list)
    variants: List[RoomVariantPublicDTO] = Field(default_factory=list)
    gallery: List[GalleryImageDTO] = Field(default_factory=list)
    highlights: List[HighlightDTO] = Field(default_factory=list)
    faqs: List[FAQDTO] = Field(default_factory=list)
    policies: List[PolicyDTO] = Field(default_factory=list)
