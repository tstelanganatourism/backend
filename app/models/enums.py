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
    PUBLIC = 'PUBLIC'
    AGENT = 'AGENT'
    OFFLINE = 'OFFLINE'

class BookingStatus(str, enum.Enum):
    PENDING = 'PENDING'
    CONFIRMED = 'CONFIRMED'
    PENDING_CANCELLATION = 'PENDING_CANCELLATION'
    CANCELLED = 'CANCELLED'
    REFUNDED = 'REFUNDED'
    FAILED = 'FAILED'

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
