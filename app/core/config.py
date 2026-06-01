from typing import Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    ENVIRONMENT: str = "development"
    PROJECT_NAME: str = "Papikondalu Tourism Booking API"
    CORS_ORIGINS: list[str] = ["https://tsboattourism.org", "https://www.tsboattourism.org"]
    ALLOWED_HOSTS: list[str] = ["tsboattourism.org", "www.tsboattourism.org", "127.0.0.1", "localhost"]
    
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
    FRONTEND_URL: str
    GOOGLE_REDIRECT_URI: str
    
    # Cloudinary
    CLOUDINARY_CLOUD_NAME: Optional[str] = None
    CLOUDINARY_API_KEY: Optional[str] = None
    CLOUDINARY_API_SECRET: Optional[str] = None

    # Cloudflare R2
    R2_ACCOUNT_ID: Optional[str] = None
    R2_ACCESS_KEY_ID: Optional[str] = None
    R2_SECRET_ACCESS_KEY: Optional[str] = None
    R2_BUCKET_NAME: Optional[str] = None
    
    # Razorpay
    RAZORPAY_KEY_ID: Optional[str] = None
    RAZORPAY_KEY_SECRET: Optional[str] = None
    
    # Brevo (Email Sending)
    BREVO_API_KEY: Optional[str] = None
    BREVO_FROM_EMAIL: str = "bookings@tsboattourism.org"
    
    # Google Auth
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    # Sentry
    SENTRY_DSN: Optional[str] = None
    
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    @field_validator("ENVIRONMENT")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        return value.strip().lower()

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
