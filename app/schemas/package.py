from typing import Optional, List
from pydantic import Field
from decimal import Decimal
from app.schemas.base import AppBaseModel, TimestampSchema
from app.models.enums import PackageType, RegionType, PolicyType, PublishStatus, TransportOptionType

class SEOSchema(AppBaseModel):
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    og_image_url: Optional[str] = None
    canonical_url: Optional[str] = None

class PackageVariantBase(AppBaseModel):
    title: str
    adult_price: Decimal
    child_price: Decimal
    weekend_adult_price: Optional[Decimal] = None
    weekend_child_price: Optional[Decimal] = None
    # Student package pricing
    student_price: Optional[Decimal] = None
    weekend_student_price: Optional[Decimal] = None
    is_active: bool = True

class PackageTransportOptionBase(AppBaseModel):
    title: str
    type: TransportOptionType
    capacity: int = 1
    adult_price: Optional[Decimal] = None
    child_price: Optional[Decimal] = None
    weekend_adult_price: Optional[Decimal] = None
    weekend_child_price: Optional[Decimal] = None
    # Student pricing for SHARED transport
    student_price: Optional[Decimal] = None
    weekend_student_price: Optional[Decimal] = None
    fixed_price: Optional[Decimal] = None
    weekend_fixed_price: Optional[Decimal] = None

class PackageTransportOptionResponse(PackageTransportOptionBase, TimestampSchema):
    id: int

class PackageVariantResponse(PackageVariantBase, TimestampSchema):
    id: int

class PackageGalleryImageResponse(AppBaseModel):
    id: int
    image_url: str
    alt_text: Optional[str] = None
    is_cover: bool
    category: Optional[str] = None
    sort_order: int

class PackageItineraryDayResponse(AppBaseModel):
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

class PackageHighlightResponse(AppBaseModel):
    id: int
    title: str
    icon: Optional[str] = None
    sort_order: int

class PackageInclusionResponse(AppBaseModel):
    id: int
    label: str
    icon: Optional[str] = None
    sort_order: int

class PackageExclusionResponse(AppBaseModel):
    id: int
    label: str
    icon: Optional[str] = None
    sort_order: int

class PackageBoardingPointResponse(AppBaseModel):
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

class PackageFAQResponse(AppBaseModel):
    id: int
    question: str
    answer: str
    sort_order: int

class PackagePolicyResponse(AppBaseModel):
    id: int
    type: PolicyType
    title: str
    description: str
    sort_order: int

# Nested Inputs for single-transaction update
class PackageVariantInput(AppBaseModel):
    id: Optional[int] = None
    title: str
    adult_price: Decimal
    child_price: Decimal
    weekend_adult_price: Optional[Decimal] = None
    weekend_child_price: Optional[Decimal] = None
    # Student pricing
    student_price: Optional[Decimal] = None
    weekend_student_price: Optional[Decimal] = None
    is_active: bool = True

class PackageTransportOptionInput(AppBaseModel):
    id: Optional[int] = None
    title: str
    type: TransportOptionType
    capacity: int = 1
    adult_price: Optional[Decimal] = None
    child_price: Optional[Decimal] = None
    weekend_adult_price: Optional[Decimal] = None
    weekend_child_price: Optional[Decimal] = None
    # Student pricing for SHARED transport
    student_price: Optional[Decimal] = None
    weekend_student_price: Optional[Decimal] = None
    fixed_price: Optional[Decimal] = None
    weekend_fixed_price: Optional[Decimal] = None

class PackageGalleryImageInput(AppBaseModel):
    id: Optional[int] = None
    image_url: str
    alt_text: Optional[str] = None
    is_cover: bool = False
    category: Optional[str] = None
    sort_order: int = 0

class PackageItineraryDayInput(AppBaseModel):
    id: Optional[int] = None
    day_number: int
    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    timing: Optional[str] = None
    duration_at_stop: Optional[str] = None
    image_url: Optional[str] = None
    meal_included: bool = False
    sort_order: int = 0

class PackageHighlightInput(AppBaseModel):
    id: Optional[int] = None
    title: str
    icon: Optional[str] = None
    sort_order: int = 0

class PackageInclusionInput(AppBaseModel):
    id: Optional[int] = None
    label: str
    icon: Optional[str] = None
    sort_order: int = 0

class PackageExclusionInput(AppBaseModel):
    id: Optional[int] = None
    label: str
    icon: Optional[str] = None
    sort_order: int = 0

class PackageBoardingPointInput(AppBaseModel):
    id: Optional[int] = None
    title: str
    address: Optional[str] = None
    map_url: Optional[str] = None
    departure_time: Optional[str] = None
    landmark: Optional[str] = None
    contact_number: Optional[str] = None
    pickup_instructions: Optional[str] = None
    return_drop_info: Optional[str] = None
    sort_order: int = 0

class PackageFAQInput(AppBaseModel):
    id: Optional[int] = None
    question: str
    answer: str
    sort_order: int = 0

class PackagePolicyInput(AppBaseModel):
    id: Optional[int] = None
    type: PolicyType
    title: str
    description: str
    sort_order: int = 0

class PackageBase(AppBaseModel):
    title: str
    slug: str
    type: PackageType
    region: Optional[RegionType] = None
    description: Optional[str] = None
    duration: Optional[str] = None
    place: Optional[str] = None
    cover_image_url: Optional[str] = None
    brochure_pdf_url: Optional[str] = None
    generated_brochure_url: Optional[str] = None
    order_priority: int = 0
    has_transport: bool = False
    has_refreshments: bool = False
    refreshment_adult_price: Optional[Decimal] = None
    refreshment_child_price: Optional[Decimal] = None
    # Student package
    is_student_package: bool = False
    refreshment_student_price: Optional[Decimal] = None
    is_featured: bool = False
    is_active: bool = True
    status: PublishStatus = PublishStatus.DRAFT
    min_passengers: int = Field(1, ge=1)

class PackageResponse(PackageBase, TimestampSchema):
    id: int
    variants: List[PackageVariantResponse] = []
    transport_options: List[PackageTransportOptionResponse] = []
    starting_price: Optional[Decimal] = None
    generated_brochure_url: Optional[str] = None
    active_booking_count: Optional[int] = 0

class PackageDetailResponse(PackageResponse, SEOSchema):
    gallery: List[PackageGalleryImageResponse] = []
    itinerary: List[PackageItineraryDayResponse] = []
    highlights: List[PackageHighlightResponse] = []
    inclusions: List[PackageInclusionResponse] = []
    exclusions: List[PackageExclusionResponse] = []
    boarding_points: List[PackageBoardingPointResponse] = []
    faqs: List[PackageFAQResponse] = []
    policies: List[PackagePolicyResponse] = []

class PackageCreate(PackageBase, SEOSchema):
    variants: List[PackageVariantInput] = []
    transport_options: List[PackageTransportOptionInput] = []
    gallery: List[PackageGalleryImageInput] = []
    itinerary: List[PackageItineraryDayInput] = []
    highlights: List[PackageHighlightInput] = []
    inclusions: List[PackageInclusionInput] = []
    exclusions: List[PackageExclusionInput] = []
    boarding_points: List[PackageBoardingPointInput] = []
    faqs: List[PackageFAQInput] = []
    policies: List[PackagePolicyInput] = []

class PackageUpdate(AppBaseModel):
    title: Optional[str] = None
    slug: Optional[str] = None
    type: Optional[PackageType] = None
    region: Optional[RegionType] = None
    description: Optional[str] = None
    duration: Optional[str] = None
    place: Optional[str] = None
    cover_image_url: Optional[str] = None
    brochure_pdf_url: Optional[str] = None
    generated_brochure_url: Optional[str] = None
    order_priority: Optional[int] = None
    has_transport: Optional[bool] = None
    has_refreshments: Optional[bool] = None
    refreshment_adult_price: Optional[Decimal] = None
    refreshment_child_price: Optional[Decimal] = None
    # Student package
    is_student_package: Optional[bool] = None
    refreshment_student_price: Optional[Decimal] = None
    is_featured: Optional[bool] = None
    is_active: Optional[bool] = None
    status: Optional[PublishStatus] = None
    min_passengers: Optional[int] = Field(None, ge=1)
    
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    og_image_url: Optional[str] = None
    canonical_url: Optional[str] = None

    variants: Optional[List[PackageVariantInput]] = None
    transport_options: Optional[List[PackageTransportOptionInput]] = None
    gallery: Optional[List[PackageGalleryImageInput]] = None
    itinerary: Optional[List[PackageItineraryDayInput]] = None
    highlights: Optional[List[PackageHighlightInput]] = None
    inclusions: Optional[List[PackageInclusionInput]] = None
    exclusions: Optional[List[PackageExclusionInput]] = None
    boarding_points: Optional[List[PackageBoardingPointInput]] = None
    faqs: Optional[List[PackageFAQInput]] = None
    policies: Optional[List[PackagePolicyInput]] = None


class PackagePaginatedResponse(AppBaseModel):
    items: List[PackageResponse]
    total: int
    page: int
    size: int
