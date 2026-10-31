import uuid
import re
import io
import logging
from pathlib import Path
from PIL import Image as PILImage
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from app.core.config import settings
from app.services.r2_storage import r2_service
from app.middleware.auth import require_admin
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/media",
    tags=["Admin - Media Management"],
    dependencies=[Depends(require_admin)]
)

# Optional Cloudinary init if credentials are provided
if settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET:
    try:
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(
            cloud_name=settings.CLOUDINARY_CLOUD_NAME,
            api_key=settings.CLOUDINARY_API_KEY,
            api_secret=settings.CLOUDINARY_API_SECRET,
            secure=True
        )
    except Exception as e:
        logger.warning(f"Cloudinary config warning: {e}")

@router.get("/storage-status")
async def get_storage_status(
    current_admin: User = Depends(require_admin)
):
    """
    Returns the configuration status of all storage providers.
    Use this endpoint from the Render logs or admin panel to diagnose upload issues.
    """
    return {
        "r2": {
            "configured": r2_service.is_configured,
            "account_id_set": bool(settings.R2_ACCOUNT_ID),
            "access_key_set": bool(settings.R2_ACCESS_KEY_ID),
            "secret_key_set": bool(settings.R2_SECRET_ACCESS_KEY),
            "bucket_name": settings.R2_BUCKET_NAME or "(not set)",
            "custom_domain": settings.R2_CUSTOM_DOMAIN or "(not set)",
        },
        "cloudinary": {
            "configured": bool(settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET),
            "cloud_name": settings.CLOUDINARY_CLOUD_NAME or "(not set)",
        },
        "recommendation": (
            "R2 is the primary storage. Ensure R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and R2_BUCKET_NAME are set in Render Environment Variables."
            if not r2_service.is_configured else "R2 is configured and will be used as primary storage."
        )
    }


@router.post("/upload")
async def upload_media(
    file: UploadFile = File(...),
    current_admin: User = Depends(require_admin)
):
    """
    Server-mediated file upload with multi-tier storage fallback:
    1. Cloudflare R2 (Primary high-speed CDN, custom domain media.tstelanganatourism.com)
    2. Cloudinary (Secondary cloud fallback)
    3. Local /static/uploads (Guaranteed offline/emergency fallback)
    Supports: images (JPG, PNG, WEBP, GIF, AVIF, SVG), videos (MP4, WEBM, MOV), PDFs.
    """
    allowed_image_types = [
        "image/jpeg", "image/jpg", "image/png", "image/webp", 
        "image/gif", "image/avif", "image/svg+xml"
    ]
    allowed_video_types = [
        "video/mp4", "video/webm", "video/quicktime", "video/x-msvideo"
    ]
    allowed_types = allowed_image_types + allowed_video_types + ["application/pdf"]

    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only image files (JPG, PNG, WEBP, GIF, AVIF, SVG), video files (MP4, WEBM, MOV), and PDF brochures are allowed."
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

    original_filename = file.filename or "uploaded_media"
    ext = original_filename.split('.')[-1].lower() if '.' in original_filename else 'jpg'
    safe_base = re.sub(r'[^a-zA-Z0-9_-]', '_', original_filename.rsplit('.', 1)[0])[:40]
    unique_id = uuid.uuid4().hex[:12]
    unique_filename = f"{safe_base}_{unique_id}.{ext}"

    # Determine resource type and folder
    if file.content_type == "application/pdf":
        resource_type = "raw"
        folder = "ts_tours/brochures"
    elif file.content_type in allowed_video_types:
        resource_type = "video"
        folder = "ts_tours/videos"
    else:
        resource_type = "image"
        folder = "ts_tours"

    object_key = f"{folder}/{unique_filename}"

    # Extract dimensions if it is an image
    width, height = None, None
    if resource_type == "image" and file.content_type != "image/svg+xml":
        try:
            with PILImage.open(io.BytesIO(contents)) as pil_img:
                width, height = pil_img.size
        except Exception:
            pass

    r2_error_msg = None
    cloudinary_error_msg = None

    # 1. Primary: Cloudflare R2
    if r2_service.is_configured:
        try:
            await r2_service.upload_file(contents, object_key, content_type=file.content_type)
            base_url = (settings.R2_CUSTOM_DOMAIN or settings.R2_PUBLIC_URL or "https://media.tstelanganatourism.com").rstrip("/")
            public_url = f"{base_url}/{object_key}"
            logger.info(f"Successfully uploaded media to Cloudflare R2: {public_url}")
            return {
                "url": public_url,
                "public_id": object_key,
                "format": ext,
                "resource_type": resource_type,
                "width": width,
                "height": height
            }
        except Exception as r2_err:
            r2_error_msg = str(r2_err)
            logger.error(f"Cloudflare R2 upload failed, trying fallback: {r2_err}")
    else:
        r2_error_msg = "R2 credentials not configured"
        logger.warning("R2 storage not configured — skipping R2 upload")

    # 2. Secondary: Cloudinary (only if credentials are set AND the account is likely usable)
    if settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET:
        try:
            import cloudinary
            import cloudinary.uploader
            cloudinary_resource = "raw" if resource_type == "raw" else ("video" if resource_type == "video" else "image")
            upload_result = cloudinary.uploader.upload(
                contents,
                folder=folder,
                resource_type=cloudinary_resource,
                public_id=f"{safe_base}_{unique_id}"
            )
            c_url = upload_result.get("secure_url") or upload_result.get("url")
            if c_url:
                logger.info(f"Successfully uploaded media to Cloudinary: {c_url}")
                return {
                    "url": c_url,
                    "public_id": upload_result.get("public_id"),
                    "format": upload_result.get("format", ext),
                    "resource_type": resource_type,
                    "width": upload_result.get("width", width),
                    "height": upload_result.get("height", height)
                }
        except Exception as c_err:
            cloudinary_error_msg = str(c_err)
            logger.warning(f"Cloudinary upload failed: {c_err}")
    else:
        cloudinary_error_msg = "Cloudinary credentials not configured"

    # 3. Local disk fallback (/static/uploads)
    # NOTE: This is a last-resort fallback. Files saved here are ephemeral on Render
    # (lost on redeploy). Prefer fixing R2 credentials in Render dashboard.
    try:
        upload_dir = Path("app/static/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        local_filename = f"{resource_type}_{unique_filename}"
        local_path = upload_dir / local_filename
        with open(local_path, "wb") as f:
            f.write(contents)

        # Use absolute backend URL so cross-origin frontends (Vercel/Next.js) can display the image.
        # A relative path like /static/... only works when the client is on the same domain as the backend.
        backend_base = (settings.R2_CUSTOM_DOMAIN or "").rstrip("/")
        # Fallback: derive backend URL from known Render service URL or configured frontend
        if not backend_base or "media.tstelanganatourism" not in backend_base:
            backend_base = "https://backend-st7o.onrender.com"
        local_url = f"{backend_base}/static/uploads/{local_filename}"
        logger.warning(
            f"[FALLBACK] Saved media locally. This file is EPHEMERAL and will be lost on redeploy. "
            f"Please configure R2 credentials in Render dashboard. URL: {local_url}"
        )
        return {
            "url": local_url,
            "public_id": local_filename,
            "format": ext,
            "resource_type": resource_type,
            "width": width,
            "height": height
        }
    except Exception as local_err:
        logger.critical(f"All media upload tiers failed. R2: {r2_error_msg} | Cloudinary: {cloudinary_error_msg} | Local: {local_err}")

        # Build a clear, actionable error message for the admin
        if r2_error_msg and "credentials" not in r2_error_msg.lower():
            detail = f"Cloud storage upload failed. Please check that R2 credentials (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME) are set correctly in the Render environment dashboard."
        elif cloudinary_error_msg and "disabled" in cloudinary_error_msg.lower():
            detail = "Upload failed: Cloudinary account is disabled. Please re-activate your Cloudinary account or ensure R2 credentials are correctly configured in the Render dashboard."
        else:
            detail = f"Upload failed across all storage providers. R2: {r2_error_msg or 'not tried'} | Cloudinary: {cloudinary_error_msg or 'not tried'} | Local: {str(local_err)}"

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail
        )
