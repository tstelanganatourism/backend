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
    Supports: images (JPG, PNG, WEBP, GIF), videos (MP4, WEBM, MOV), PDFs.
    """
    if not (settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Media storage service is not configured."
        )
        
    # Validate file type
    allowed_image_types = ["image/jpeg", "image/png", "image/webp", "image/gif"]
    allowed_video_types = ["video/mp4", "video/webm", "video/quicktime", "video/x-msvideo"]
    allowed_types = allowed_image_types + allowed_video_types + ["application/pdf"]

    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image files (JPG, PNG, WEBP, GIF), video files (MP4, WEBM, MOV), and PDF brochures are allowed."
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
                "resource_type": "raw",
                "width": None,
                "height": None
            }
        elif file.content_type in allowed_video_types:
            # Route videos to Cloudinary with video resource type
            upload_result = cloudinary.uploader.upload(
                file.file,
                folder="ts_tours/videos",
                resource_type="video",
                public_id=None,
                eager=[{"width": 800, "height": 450, "crop": "fill", "format": "jpg"}],
                eager_async=True,
            )
            return {
                "url": upload_result.get("secure_url"),
                "public_id": upload_result.get("public_id"),
                "format": upload_result.get("format", "mp4"),
                "resource_type": "video",
                "thumbnail_url": (upload_result.get("eager") or [{}])[0].get("secure_url"),
                "duration": upload_result.get("duration"),
                "width": upload_result.get("width"),
                "height": upload_result.get("height")
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
                "resource_type": "image",
                "width": upload_result.get("width"),
                "height": upload_result.get("height")
            }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upload failed: {str(e)}"
        )
