import time
import warnings
import cloudinary
import cloudinary.uploader
import cloudinary.utils
from app.core.config import settings

# Initialize Cloudinary configuration
if settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True
    )
else:
    warnings.warn("Cloudinary credentials are not configured! Private uploads will fail.")

def upload_private_passenger_id(file_contents: bytes, filename: str) -> dict:
    """
    Upload passenger ID document to Cloudinary as a private resource.
    This prevents public URL access and forces signed URL authorization.
    """
    if not (settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET):
        raise RuntimeError("Media storage service is not configured.")

    # Upload with type='private' and folder='passengers'
    upload_result = cloudinary.uploader.upload(
        file_contents,
        folder="passengers",
        public_id=f"passenger_id_{int(time.time())}_{filename.split('.')[0]}",
        type="private",
        resource_type="image"
    )
    return {
        "public_id": upload_result.get("public_id"),
        "secure_url": upload_result.get("secure_url"),
        "format": upload_result.get("format")
    }

def generate_signed_passenger_id_url(public_id: str, expires_in_seconds: int = 60) -> str:
    """
    Generate a secure, cryptographically signed Cloudinary download URL
    that expires after a short duration (defaults to 60 seconds).
    """
    if not public_id:
        return ""
    
    # Calculate expiration time in seconds since epoch
    expires_at = int(time.time()) + expires_in_seconds
    
    # Generate signed URL
    signed_url, _ = cloudinary.utils.cloudinary_url(
        public_id,
        type="private",
        sign_url=True,
        expires_at=expires_at,
        secure=True
    )
    return signed_url
