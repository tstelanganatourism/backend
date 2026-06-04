import enum

class UserRole(str, enum.Enum):
    ADMIN = 'ADMIN'
    AGENT = 'AGENT'
    USER = 'USER'

class PublishStatus(str, enum.Enum):
    DRAFT = 'DRAFT'
    PUBLISHED = 'PUBLISHED'
    ARCHIVED = 'ARCHIVED'

class DocumentGenerationStatus(str, enum.Enum):
    AVAILABLE = 'AVAILABLE'
    QUEUED = 'QUEUED'
    GENERATING = 'GENERATING'
    FAILED = 'FAILED'
    MISSING = 'MISSING'

class AccountStatus(str, enum.Enum):
    ACTIVE = 'ACTIVE'
    BLOCKED = 'BLOCKED'
    DISABLED = 'DISABLED'

class GenderType(str, enum.Enum):
    MALE = 'MALE'
    FEMALE = 'FEMALE'
    OTHER = 'OTHER'

class BookingSource(str, enum.Enum):
    USER = 'USER'
    AGENT = 'AGENT'
    ADMIN = 'ADMIN'
    ADMIN_DIRECT = 'ADMIN_DIRECT'
    PUBLIC = 'USER' # Backwards compatibility alias

class BookingStatus(str, enum.Enum):
    PENDING = 'PENDING'
    PARTIAL_PAID = 'PARTIAL_PAID'
    FULLY_PAID = 'FULLY_PAID'
    CANCELLED = 'CANCELLED'
    REFUNDED = 'REFUNDED'

class PaymentStatus(str, enum.Enum):
    CREATED = 'CREATED'
    CAPTURED = 'CAPTURED'
    FAILED = 'FAILED'
    REFUNDED = 'REFUNDED'

class PackageType(str, enum.Enum):
    TOUR = 'TOUR'
    TRIP = 'TRIP'

class RegionType(str, enum.Enum):
    AP = 'AP'
    TS = 'TS'

class TransportOptionType(str, enum.Enum):
    SHARED = 'SHARED'
    SEPARATE_VEHICLE = 'SEPARATE_VEHICLE'

class CancellationStatus(str, enum.Enum):
    PENDING = 'PENDING'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    REFUNDED = 'REFUNDED'

class PolicyType(str, enum.Enum):
    CANCELLATION = 'CANCELLATION'
    CHECK_IN_OUT = 'CHECK_IN_OUT'
    TRAVEL_RULES = 'TRAVEL_RULES'
    GENERAL = 'GENERAL'
    SAFETY = 'SAFETY'
    LUGGAGE = 'LUGGAGE'
    FOOD = 'FOOD'
    WEATHER = 'WEATHER'
    BOARDING = 'BOARDING'
    STAY_RULES = 'STAY_RULES'
    REFUND = 'REFUND'
    CHILD_POLICY = 'CHILD_POLICY'
    PETS = 'PETS'
    SMOKING = 'SMOKING'
    OTHER = 'OTHER'

# ─── Promotion Enums (Phase-3) ────────────────────────────────────────────────

class PromotionType(str, enum.Enum):
    FLAT_DISCOUNT = 'FLAT_DISCOUNT'
    PERCENT_DISCOUNT = 'PERCENT_DISCOUNT'
    INFORMATIONAL = 'INFORMATIONAL'
    FREE_SERVICE = 'FREE_SERVICE'
    CAMPAIGN = 'CAMPAIGN'

class PromotionTarget(str, enum.Enum):
    ALL = 'ALL'
    TOURS_ONLY = 'TOURS_ONLY'
    TRIPS_ONLY = 'TRIPS_ONLY'
    ROOMS_ONLY = 'ROOMS_ONLY'
    AP_REGION = 'AP_REGION'
    TS_REGION = 'TS_REGION'
    SPECIFIC_PACKAGES = 'SPECIFIC_PACKAGES'

class PromotionBadge(str, enum.Enum):
    NONE = 'NONE'
    LIMITED_TIME = 'LIMITED_TIME'
    BESTSELLER = 'BESTSELLER'
    NEW_OFFER = 'NEW_OFFER'
    FESTIVAL_OFFER = 'FESTIVAL_OFFER'
    SUMMER_SPECIAL = 'SUMMER_SPECIAL'
