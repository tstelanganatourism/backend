from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional

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
    current_user: Optional[User] = Depends(get_current_user_optional)
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
    if req.object_key.startswith("private/tickets/") or req.object_key.startswith("private/invoices/"):
        # Strict auth required
        if not current_user:
            raise HTTPException(status_code=401, detail="Authentication required for this document")
            
        # TODO: Add logic to check if current_user actually owns the booking related to this ticket
        # For now, allow if admin
        if current_user.role != UserRole.ADMIN:
            # We will implement strict ownership check later when checkout is built.
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
