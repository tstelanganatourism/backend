from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ENVIRONMENT: str = "development"
    PROJECT_NAME: str = "TS Boat Tourism Booking API"
    CORS_ORIGINS: list[str] = ["https://tstelanganatourism.com", "https://www.tstelanganatourism.com"]
    ALLOWED_HOSTS: list[str] = ["*"]
    
    # Security
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30   # shorter for admin
    ADMIN_REFRESH_TOKEN_EXPIRE_HOURS: int = 720    # 30 days for admin
    ADMIN_OTP_EXPIRE_SECONDS: int = 300           # 5-minute OTP window
    
    # Database
    DATABASE_URL: str
    SQL_ECHO: bool = False
    
    # Redis
    REDIS_URL: str
    
    # Frontend (for OAuth redirects and CORS)
    FRONTEND_URL: str = "https://tstelanganatourism.com"
    GOOGLE_REDIRECT_URI: Optional[str] = "https://tstelanganatourism.com/auth/callback/google"
    
    # Cloudinary
    CLOUDINARY_CLOUD_NAME: Optional[str] = None
    CLOUDINARY_API_KEY: Optional[str] = None
    CLOUDINARY_API_SECRET: Optional[str] = None

    # PhonePe
    PHONEPE_MERCHANT_ID: Optional[str] = None
    PHONEPE_CLIENT_ID: Optional[str] = None
    PHONEPE_CLIENT_SECRET: Optional[str] = None
    PHONEPE_CLIENT_VERSION: str = "1"
    PHONEPE_SALT_KEY: Optional[str] = None
    PHONEPE_SALT_INDEX: int = 1
    PHONEPE_ENV: str = "PRODUCTION"

    
    # Brevo (Email Sending)
    BREVO_API_KEY: Optional[str] = None  # Legacy fallback
    BREVO_API_KEY_USER: Optional[str] = None
    BREVO_API_KEY_ADMIN: Optional[str] = None
    BREVO_API_KEY_BACKUP: Optional[str] = None
    BREVO_FROM_EMAIL: str = "tstelanganatourism@gmail.com"
    BREVO_FROM_EMAIL_USER: Optional[str] = None
    BREVO_FROM_EMAIL_ADMIN: Optional[str] = None
    BREVO_FROM_EMAIL_BACKUP: Optional[str] = None
    
    # Google Auth
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    # Sentry
    SENTRY_DSN: Optional[str] = None
    
    # MSG91 SMS Configuration
    MSG91_AUTH_KEY: Optional[str] = None
    MSG91_OTP_TEMPLATE_ID: str = "6a30e5920770a4e4380eabf2"
    MSG91_ROOM_CONFIRM_TEMPLATE_ID: str = "6a30e6485b3a7bcbed0723b2"
    MSG91_ROOM_REMINDER_TEMPLATE_ID: str = "6a30e6b14dbba838d40e87f3"
    MSG91_CONFIRMATION_FULL_TEMPLATE_ID: str = "6a30e7286af9ab609f05e032"
    MSG91_CONFIRMATION_PARTIAL_TEMPLATE_ID: str = "6a30e78573fa5bd0530bb953"
    MSG91_TRAVEL_REMINDER_TEMPLATE_ID: str = "6a30e7e746fbb45b8b0fb2c2"
    
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    @field_validator("ENVIRONMENT")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def convert_postgres_to_asyncpg(cls, value: str) -> str:
        if not value:
            return value
        if value.startswith("postgres://"):
            value = value.replace("postgres://", "postgresql+asyncpg://", 1)
        elif value.startswith("postgresql://") and not value.startswith("postgresql+asyncpg://"):
            value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
        
        # asyncpg does not accept 'sslmode' or 'channel_binding'
        if "sslmode=" in value:
            value = value.replace("sslmode=", "ssl=")
        if "&channel_binding=require" in value:
            value = value.replace("&channel_binding=require", "")
        if "?channel_binding=require" in value:
            value = value.replace("?channel_binding=require", "?")
            
        return value

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", mode="before")
    @classmethod
    def parse_csv_list(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.ENVIRONMENT == "production":
            if self.SECRET_KEY == "generate-a-secure-secret-key-here":
                raise ValueError("SECRET_KEY must be changed for production")
            if "*" in self.CORS_ORIGINS:
                raise ValueError("CORS_ORIGINS cannot include '*' in production")
        return self

settings = Settings()
# Force reload for sandbox settings update