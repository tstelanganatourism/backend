"""
Inventory schemas — Phase 3.3

Covers:
  - Admin read/write for PackageVariantInventory rows
  - Public availability response for package detail page
"""
from typing import Optional, List
from datetime import date
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field


class AppBaseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ─── Admin Schemas ────────────────────────────────────────────────────────────

class PackageInventoryGenerateRequest(AppBaseModel):
    """Generate inventory rows for a variant over a date range."""
    variant_id: int
    from_date: date
    to_date: date
    total_capacity: int = Field(default=500, ge=1, le=10000)


class PackageInventoryUpdateRequest(AppBaseModel):
    """Partial update for a single (variant_id, date) inventory row."""
    total_capacity: Optional[int] = Field(None, ge=1, le=10000)
    is_closed: Optional[bool] = None
    price_override: Optional[Decimal] = Field(None)


class PackageInventoryRow(AppBaseModel):
    """Admin-facing inventory row with all control fields."""
    id: int
    variant_id: int
    date: date
    total_capacity: int
    booked_count: int
    available_seats: int  # computed: total_capacity - booked_count
    is_closed: bool
    price_override: Optional[Decimal] = None

    @classmethod
    def from_orm_row(cls, row) -> "PackageInventoryRow":
        return cls(
            id=row.id,
            variant_id=row.variant_id,
            date=row.date,
            total_capacity=row.total_capacity,
            booked_count=row.booked_count,
            available_seats=max(0, row.total_capacity - row.booked_count),
            is_closed=row.is_closed,
            price_override=row.price_override,
        )


class PackageInventoryGenerateResponse(AppBaseModel):
    created: int
    skipped: int  # rows that already existed
    message: str


# ─── Public Schemas ───────────────────────────────────────────────────────────

class PublicTransportDateAvailability(AppBaseModel):
    """Transport availability for a single date."""
    option_id: int
    remaining: int
    is_closed: bool
    price_override: Optional[Decimal] = None

class PublicDateAvailability(AppBaseModel):
    """Single date slot returned in the public availability endpoint."""
    date: date
    variant_id: int
    variant_title: str
    adult_price: Decimal
    child_price: Decimal
    student_price: Optional[Decimal] = None
    # Effective prices (price_override if set, else variant base price)
    effective_adult_price: Decimal
    effective_child_price: Decimal
    effective_student_price: Optional[Decimal] = None
    available_seats: int
    is_closed: bool
    # Derived status for easy frontend consumption
    status: str  # "OPEN" | "CLOSED" | "SOLD_OUT" | "NO_INVENTORY"
    transport_availability: Optional[List[PublicTransportDateAvailability]] = None


class PublicPackageAvailabilityResponse(AppBaseModel):
    package_id: int
    slug: str
    month: str  # "YYYY-MM"
    dates: List[PublicDateAvailability]


# ─── Room Inventory Schemas ──────────────────────────────────────────────────

class RoomSlotCapacityRequest(AppBaseModel):
    slot_start: str
    slot_end: str
    total_rooms: int = Field(..., ge=0, le=10000)


class RoomInventoryGenerateRequest(AppBaseModel):
    room_variant_id: int
    from_date: date
    to_date: date
    override_total_rooms: Optional[int] = Field(None, ge=0, le=10000)
    slot_capacities: Optional[List[RoomSlotCapacityRequest]] = None


class RoomInventoryUpdateRequest(AppBaseModel):
    total_rooms: Optional[int] = Field(None, ge=0, le=10000)
    is_closed: Optional[bool] = None


class RoomInventoryRow(AppBaseModel):
    id: int
    room_variant_id: int
    date: date
    slot_start: str
    slot_end: str
    total_rooms: int
    booked_rooms: int
    available_rooms: int
    is_closed: bool


class RoomInventoryGenerateResponse(AppBaseModel):
    created: int
    skipped: int
    message: str


# ─── Transport Inventory Schemas ─────────────────────────────────────────────

class TransportInventoryGenerateRequest(AppBaseModel):
    """Bulk-generate transport inventory rows for a date range."""
    package_id: int
    from_date: date
    to_date: date
    # Per transport-option counts: {transport_option_id: available_count}
    # If not provided, defaults to the transport option's capacity
    option_counts: Optional[dict] = None  # {str(option_id): int}


class TransportInventoryUpdateRequest(AppBaseModel):
    """Partial update for a single transport inventory slot."""
    available_count: Optional[int] = Field(None, ge=0, le=9999)
    capacity: Optional[int] = Field(None, ge=1, le=9999)
    is_closed: Optional[bool] = None
    price_override: Optional[Decimal] = None


class TransportInventoryRow(AppBaseModel):
    """Admin-facing transport inventory row."""
    id: int
    transport_option_id: int
    transport_option_title: str
    transport_option_type: str  # 'SHARED' | 'SEPARATE_VEHICLE'
    transport_option_capacity: int
    date: date
    available_count: int
    booked_count: int
    remaining: int  # available_count - booked_count
    is_closed: bool
    price_override: Optional[Decimal] = None

    @classmethod
    def from_orm_with_option(cls, row, option) -> "TransportInventoryRow":
        t_type = str(option.type.value) if hasattr(option.type, 'value') else str(option.type)
        is_shared = t_type != 'SEPARATE_VEHICLE'
        total_capacity = (row.available_count * (option.capacity or 1)) if is_shared else row.available_count
        
        return cls(
            id=row.id,
            transport_option_id=row.transport_option_id,
            transport_option_title=option.title,
            transport_option_type=t_type,
            transport_option_capacity=int(option.capacity or 1),
            date=row.date,
            available_count=row.available_count,
            booked_count=row.booked_count,
            remaining=max(0, total_capacity - row.booked_count),
            is_closed=row.is_closed,
            price_override=row.price_override,
        )


class TransportInventoryGenerateResponse(AppBaseModel):
    created: int
    skipped: int
    message: str


# ─── Bulk Action Schemas ─────────────────────────────────────────────────────

from enum import Enum

class BulkActionType(str, Enum):
    UPDATE_CAPACITY = "UPDATE_CAPACITY"
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    DELETE = "DELETE"


class InventoryBulkActionRequest(AppBaseModel):
    variant_id: int
    from_date: date
    to_date: date
    action: BulkActionType
    total_capacity: Optional[int] = Field(None, ge=1, le=10000)


class RoomInventoryBulkActionRequest(AppBaseModel):
    room_variant_id: int
    from_date: date
    to_date: date
    action: BulkActionType
    total_rooms: Optional[int] = Field(None, ge=0, le=10000)


class TransportInventoryBulkActionRequest(AppBaseModel):
    package_id: int
    from_date: date
    to_date: date
    action: BulkActionType
    option_counts: Optional[dict] = None  # {str(option_id): count} for UPDATE_CAPACITY
    
    class Config:
        use_enum_values = True
