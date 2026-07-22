import uuid
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
import cloudinary
import cloudinary.uploader
from app.core.config import settings
from app.middleware.auth import require_admin
from app.models.user import User

router = APIRouter(
    prefix="/media",
    tags=["Admin - Media Management"],
    dependencies=[Depends(require_admin)]
)

# Initialize Cloudinary if credentials are provided
if settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True
    )
else:
    # Fallback or alert if not configured
    import warnings
    warnings.warn("Cloudinary is not configured! Uploads will fail.")

@router.post("/upload")
async def upload_media(
    file: UploadFile = File(...),
    current_admin: User = Depends(require_admin)
):
    """
    Secure server-mediated file upload to Cloudinary.
    Keeps API credentials 100% secure on the server side.
    """
    if not (settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Media storage service is not configured."
        )
        
    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image files (JPG, PNG, WEBP, GIF) and PDF brochures are allowed."
        )
        
    try:
        if file.content_type == "application/pdf":
            # Route PDFs to Cloudinary
            upload_result = cloudinary.uploader.upload(
                file.file,
                folder="ts_tours/brochures",
                resource_type="auto",
                public_id=None
            )
            
            return {
                "url": upload_result.get("secure_url"),
                "public_id": upload_result.get("public_id"),
                "format": upload_result.get("format", "pdf"),
                "width": None,
                "height": None
            }
        else:
            # Route images to Cloudinary
            upload_result = cloudinary.uploader.upload(
                file.file,
                folder="ts_tours",
                resource_type="image",
                public_id=None
            )
            
            return {
                "url": upload_result.get("secure_url"),
                "public_id": upload_result.get("public_id"),
                "format": upload_result.get("format"),
                "width": upload_result.get("width"),
                "height": upload_result.get("height")
            }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upload failed: {str(e)}"
        )
