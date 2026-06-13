from app.db.base import Base
from app.models.base import BaseModel
from app.models.enums import (
    UserRole, AccountStatus, GenderType, BookingSource, BookingStatus,
    PaymentStatus, PackageType, RegionType, CancellationStatus, PolicyType,
    PromotionType, PromotionTarget, PromotionBadge, PublishStatus,
)
from app.models.user import User, AgentPackageQuota
from app.models.tag import Tag
from app.models.room import (
    Room, RoomSlotInventory, RoomGalleryImage, RoomHighlight, RoomFAQ, RoomPolicy, room_tags
)
from app.models.package import (
    Package, PackageVariant, PackageVariantInventory, package_tags,
    PackageGalleryImage, PackageItineraryDay, PackageHighlight,
    PackageInclusion, PackageExclusion, PackageBoardingPoint,
    PackageFAQ, PackagePolicy
)
from app.models.booking import Booking, BookingStayDate, BookingPassenger, CancellationRequest
from app.models.payment import Payment
from app.models.add_on import AddOn
from app.models.promotion import Promotion
from app.models.coupon import Coupon
from app.models.settings import SystemSettings, AuditLog

# Expose all models for Alembic
__all__ = [
    "Base", "BaseModel",
    "UserRole", "AccountStatus", "GenderType", "BookingSource", "BookingStatus",
    "PaymentStatus", "PackageType", "RegionType", "CancellationStatus", "PolicyType",
    "PromotionType", "PromotionTarget", "PromotionBadge", "PublishStatus",
    "User", "AgentPackageQuota", "Tag", "Room", "RoomSlotInventory", "RoomGalleryImage", "RoomHighlight", "RoomFAQ", "RoomPolicy", "room_tags",
    "Package", "PackageVariant", "PackageVariantInventory", "package_tags",
    "PackageGalleryImage", "PackageItineraryDay", "PackageHighlight",
    "PackageInclusion", "PackageExclusion", "PackageBoardingPoint",
    "PackageFAQ", "PackagePolicy",
    "Booking", "BookingStayDate", "BookingPassenger", "CancellationRequest", "Payment", "AddOn",
    "Promotion", "Coupon", "SystemSettings", "AuditLog"
]
