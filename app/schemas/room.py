from typing import Optional, List
from pydantic import Field
from decimal import Decimal
from datetime import time
from app.schemas.base import AppBaseModel, TimestampSchema
from app.models.enums import PolicyType, PublishStatus

class RoomBookingSlotSchema(AppBaseModel):
    title: str
    slot_start: str
    slot_end: str

class RoomVariantBase(AppBaseModel):
    variant_name: str
    weekday_price: Decimal
    weekend_price: Decimal
    capacity_per_room: int
    is_active: bool = True

class RoomVariantResponse(RoomVariantBase, TimestampSchema):
    id: int

class RoomGalleryImageResponse(AppBaseModel):
    id: int
    image_url: str
    alt_text: Optional[str] = None
    is_cover: bool
    sort_order: int

class RoomHighlightResponse(AppBaseModel):
    id: int
    title: str
    icon: Optional[str] = None
    sort_order: int

class RoomFAQResponse(AppBaseModel):
    id: int
    question: str
    answer: str
    sort_order: int

class RoomPolicyResponse(AppBaseModel):
    id: int
    type: PolicyType
    title: str
    description: str
    sort_order: int

# Nested Inputs for single-transaction update
class RoomVariantInput(AppBaseModel):
    id: Optional[int] = None
    variant_name: str
    weekday_price: Decimal
    weekend_price: Decimal
    capacity_per_room: int
    is_active: bool = True

class RoomGalleryImageInput(AppBaseModel):
    id: Optional[int] = None
    image_url: str
    alt_text: Optional[str] = None
    is_cover: bool = False
    sort_order: int = 0

class RoomHighlightInput(AppBaseModel):
    id: Optional[int] = None
    title: str
    icon: Optional[str] = None
    sort_order: int = 0

class RoomFAQInput(AppBaseModel):
    id: Optional[int] = None
    question: str
    answer: str
    sort_order: int = 0

class RoomPolicyInput(AppBaseModel):
    id: Optional[int] = None
    type: PolicyType
    title: str
    description: str
    sort_order: int = 0

class RoomBase(AppBaseModel):
    lodge_name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    address: Optional[str] = None
    map_url: Optional[str] = None
    facilities: Optional[List[str]] = None
    cover_image_url: Optional[str] = None
    total_rooms: int
    slot_start: time
    slot_end: time
    booking_slots: Optional[List[RoomBookingSlotSchema]] = None
    order_priority: int = 0
    is_featured: bool = False
    is_active: bool = True
    status: PublishStatus = PublishStatus.DRAFT

    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    og_image_url: Optional[str] = None
    canonical_url: Optional[str] = None

class RoomCreate(RoomBase):
    variants: List[RoomVariantInput] = []
    gallery: List[RoomGalleryImageInput] = []
    highlights: List[RoomHighlightInput] = []
    faqs: List[RoomFAQInput] = []
    policies: List[RoomPolicyInput] = []

class RoomResponse(RoomBase, TimestampSchema):
    id: int
    variants: List[RoomVariantResponse] = []
    starting_price: Optional[Decimal] = None

class RoomDetailResponse(RoomResponse):
    gallery: List[RoomGalleryImageResponse] = []
    highlights: List[RoomHighlightResponse] = []
    faqs: List[RoomFAQResponse] = []
    policies: List[RoomPolicyResponse] = []


class RoomPaginatedResponse(AppBaseModel):
    items: List[RoomResponse]
    total: int
    page: int
    size: int
