import gc
import os
import shutil
import uuid
import re
import logging
from pathlib import Path
from PIL import Image as PILImage
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from app.core.config import settings
from app.services.r2_storage import r2_service
from app.middleware.auth import require_admin
from app.models.user import User

logger = logging.getLogger(__name__)

# Guard against malformed image decompression bombs and keep upload RAM bounded
PILImage.MAX_IMAGE_PIXELS = 50_000_000
MAX_IMAGE_UPLOAD_BYTES = 20 * 1024 * 1024   # 20 MB
MAX_PDF_UPLOAD_BYTES = 25 * 1024 * 1024     # 25 MB
MAX_VIDEO_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

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

    file_size = 0
    try:
        # Measure size on the spooled file without loading huge uploads into a Python bytes buffer
        file.file.seek(0, os.SEEK_END)
        file_size = file.file.tell()
        file.file.seek(0)

        if file_size <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

        if file.content_type == "application/pdf":
            resource_type = "raw"
            folder = "ts_tours/brochures"
            max_bytes = MAX_PDF_UPLOAD_BYTES
            label = "PDF"
        elif file.content_type in allowed_video_types:
            resource_type = "video"
            folder = "ts_tours/videos"
            max_bytes = MAX_VIDEO_UPLOAD_BYTES
            label = "Video"
        else:
            resource_type = "image"
            folder = "ts_tours"
            max_bytes = MAX_IMAGE_UPLOAD_BYTES
            label = "Image"

        if file_size > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"{label} exceeds maximum allowed size of {max_bytes // (1024 * 1024)} MB."
            )

        original_filename = file.filename or "uploaded_media"
        ext = original_filename.split('.')[-1].lower() if '.' in original_filename else 'jpg'
        safe_base = re.sub(r'[^a-zA-Z0-9_-]', '_', original_filename.rsplit('.', 1)[0])[:40]
        unique_id = uuid.uuid4().hex[:12]
        unique_filename = f"{safe_base}_{unique_id}.{ext}"
        object_key = f"{folder}/{unique_filename}"

        # Extract header-only dimensions for raster images without copying full bytes
        width, height = None, None
        if resource_type == "image" and file.content_type != "image/svg+xml":
            try:
                file.file.seek(0)
                with PILImage.open(file.file) as pil_img:
                    width, height = pil_img.size
            except Exception:
                pass
            finally:
                file.file.seek(0)

        r2_error_msg = None
        cloudinary_error_msg = None

        # 1. Primary: Cloudflare R2 (streams directly from spooled file)
        if r2_service.is_configured:
            try:
                file.file.seek(0)
                await r2_service.upload_file(
                    file.file,
                    object_key,
                    content_type=file.content_type,
                    content_length=file_size,
                )
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

        # 2. Cloudinary permanently bypassed (account disabled; R2 is primary high-speed global storage)
        cloudinary_error_msg = "Cloudinary disabled - bypassed for Cloudflare R2"

        # 3. Local disk fallback (/static/uploads) — stream in 64 KB chunks
        try:
            upload_dir = Path("app/static/uploads")
            upload_dir.mkdir(parents=True, exist_ok=True)
            local_filename = f"{resource_type}_{unique_filename}"
            local_path = upload_dir / local_filename
            file.file.seek(0)
            with open(local_path, "wb") as f:
                shutil.copyfileobj(file.file, f, length=65536)

            backend_base = (settings.R2_CUSTOM_DOMAIN or "").rstrip("/")
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

            if r2_error_msg and "credentials" not in r2_error_msg.lower():
                detail = "Cloud storage upload failed. Please check that R2 credentials (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME) are set correctly in the Render environment dashboard."
            elif cloudinary_error_msg and "disabled" in cloudinary_error_msg.lower():
                detail = "Upload failed: Cloudinary account is disabled. Please re-activate your Cloudinary account or ensure R2 credentials are correctly configured in the Render dashboard."
            else:
                detail = f"Upload failed across all storage providers. R2: {r2_error_msg or 'not tried'} | Cloudinary: {cloudinary_error_msg or 'not tried'} | Local: {str(local_err)}"

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=detail
            )
    finally:
        await file.close()
        if file_size > 1_048_576:
            gc.collect()
