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

class PublicDateAvailability(AppBaseModel):
    """Single date slot returned in the public availability endpoint."""
    date: date
    variant_id: int
    variant_title: str
    adult_price: Decimal
    child_price: Decimal
    # Effective prices (price_override if set, else variant base price)
    effective_adult_price: Decimal
    effective_child_price: Decimal
    available_seats: int
    is_closed: bool
    # Derived status for easy frontend consumption
    status: str  # "OPEN" | "CLOSED" | "SOLD_OUT" | "NO_INVENTORY"


class PublicPackageAvailabilityResponse(AppBaseModel):
    package_id: int
    slug: str
    month: str  # "YYYY-MM"
    dates: List[PublicDateAvailability]


# ─── Room Inventory Schemas ──────────────────────────────────────────────────

class RoomInventoryGenerateRequest(AppBaseModel):
    room_variant_id: int
    from_date: date
    to_date: date
    override_total_rooms: Optional[int] = Field(None, ge=0, le=10000)


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

