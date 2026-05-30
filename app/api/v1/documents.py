from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db

from app.middleware.auth import get_current_user_optional, get_current_user
from app.models.user import User
from app.models.enums import UserRole
from app.services.r2_storage import r2_service

router = APIRouter(
    prefix="/documents",
    tags=["Documents & Storage"]
)

class SignedUrlRequest(BaseModel):
    object_key: str

class SignedUrlResponse(BaseModel):
    url: str
    expires_in: int

@router.post("/signed-url", response_model=SignedUrlResponse)
async def get_signed_url(
    req: SignedUrlRequest,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """
    Generate a short-lived (15 minute) signed URL for private document access.
    Brochures can be downloaded by anyone if public, but we enforce this endpoint 
    so we can later restrict based on roles or booking ownership.
    For now, uploaded/generated brochures are accessible, but tickets/invoices require strict auth.
    """
    
    # 1. Validate object key prevents directory traversal
    if ".." in req.object_key or req.object_key.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid object key")
        
    # 2. Access Control Logic
    if req.object_key.startswith("private/invoices/"):
        if not current_user:
            raise HTTPException(status_code=401, detail="Authentication required for this document")
        if current_user.role != UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="Invoices are restricted to administrators only")

    elif req.object_key.startswith("private/tickets/"):
        # Strict auth required
        if not current_user:
            raise HTTPException(status_code=401, detail="Authentication required for this document")
            
        if current_user.role != UserRole.ADMIN:
            from sqlalchemy.future import select
            from app.models.booking import Booking
            
            stmt = select(Booking).where(
                Booking.ticket_pdf_url == req.object_key
            )
            result = await db.execute(stmt)
            booking = result.scalars().first()
            
            if not booking:
                raise HTTPException(status_code=404, detail="Associated booking not found for this document")
            
            # Check ownership
            is_owner = (booking.user_id == current_user.id or booking.agent_id == current_user.id)
            if not is_owner:
                raise HTTPException(status_code=403, detail="Not authorized to view this document")
            
    elif req.object_key.startswith("private/brochures/"):
        # Brochures are private in R2, but public users can request a signed URL to download them
        pass 
        
    else:
        raise HTTPException(status_code=400, detail="Unknown document prefix")

    try:
        url = await r2_service.generate_presigned_url(req.object_key, expires_in=900)
        return SignedUrlResponse(url=url, expires_in=900)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to generate secure URL")

from fastapi.responses import RedirectResponse

@router.get("/download")
async def download_document(
    key: str,
    filename: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Directly download a document by redirecting to a fresh presigned URL.
    Forces the browser to download (not open) by embedding Content-Disposition in the URL.
    """
    if ".." in key or key.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid object key")

    # Access control
    if key.startswith("private/invoices/"):
        if not current_user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if current_user.role != UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="Invoices are restricted to administrators")

    elif key.startswith("private/tickets/"):
        if not current_user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if current_user.role != UserRole.ADMIN:
            from sqlalchemy.future import select
            from app.models.booking import Booking
            stmt = select(Booking).where(Booking.ticket_pdf_url == key)
            result = await db.execute(stmt)
            booking = result.scalars().first()
            if not booking:
                raise HTTPException(status_code=404, detail="Booking not found for this document")
            is_owner = (booking.user_id == current_user.id or booking.agent_id == current_user.id)
            if not is_owner:
                raise HTTPException(status_code=403, detail="Not authorized to view this document")

    elif key.startswith("private/brochures/"):
        pass  # Public access allowed

    else:
        raise HTTPException(status_code=400, detail="Unknown document prefix")

    # Derive filename if not provided
    if not filename:
        filename = key.split("/")[-1]

    try:
        client = await r2_service.get_client()
        # Generate a presigned URL with Content-Disposition baked in — forces download
        generated = client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': r2_service.bucket_name,
                'Key': key,
                'ResponseContentDisposition': f'attachment; filename="{filename}"',
                'ResponseContentType': 'application/pdf',
            },
            ExpiresIn=900
        )
        import inspect as _inspect
        url = await generated if _inspect.isawaitable(generated) else generated
        return RedirectResponse(url=url, status_code=307)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to generate secure URL")

