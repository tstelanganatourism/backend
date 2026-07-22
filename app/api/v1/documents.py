from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db, AsyncSessionLocal

from app.middleware.auth import get_current_user_optional, get_current_user
from app.models.user import User
from app.models.enums import UserRole
from fastapi.responses import StreamingResponse
import httpx

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
    request: Request
):
    """
    Generate a short-lived (15 minute) signed URL for private document access.
    Brochures can be downloaded by anyone if public.
    """
    # 1. Validate object key
    if ".." in req.object_key:
        raise HTTPException(status_code=400, detail="Invalid object key")
        
    # If the key is already a full Cloudinary/HTTPS URL, return it directly.
    if req.object_key.startswith("http://") or req.object_key.startswith("https://"):
        return SignedUrlResponse(url=req.object_key, expires_in=900)

    # 2. Access Control Logic (fallback/legacy)
    if req.object_key.startswith("private/invoices/") or req.object_key.startswith("private/tickets/"):
        async with AsyncSessionLocal() as db:
            auth_header = request.headers.get("Authorization")
            credentials = None
            if auth_header and auth_header.startswith("Bearer "):
                from fastapi.security.http import HTTPAuthorizationCredentials
                credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=auth_header[7:])
            
            current_user = await get_current_user_optional(credentials=credentials, db=db)
            
            if req.object_key.startswith("private/invoices/"):
                if not current_user:
                    raise HTTPException(status_code=401, detail="Authentication required for this document")
                if current_user.role != UserRole.ADMIN:
                    raise HTTPException(status_code=403, detail="Invoices are restricted to administrators only")

            elif req.object_key.startswith("private/tickets/"):
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
                    
                    is_owner = (booking.user_id == current_user.id or booking.agent_id == current_user.id)
                    if not is_owner:
                        raise HTTPException(status_code=403, detail="Not authorized to view this document")
                        
    elif req.object_key.startswith("private/brochures/"):
        pass 
    else:
        raise HTTPException(status_code=400, detail="Unknown document prefix or invalid URL")

    # If it is a legacy key not starting with http, return a dummy/blank url or raise error as R2 is removed
    raise HTTPException(status_code=404, detail="Legacy R2 keys are no longer accessible.")


@router.get("/download")
async def download_document(
    key: str,
    request: Request,
    filename: Optional[str] = None
):
    """
    Directly download a document by proxying/streaming it from Cloudinary/external URL.
    """
    if ".." in key:
        raise HTTPException(status_code=400, detail="Invalid object key")

    # If it's a full URL (new Cloudinary storage flow)
    if key.startswith("http://") or key.startswith("https://"):
        if not filename:
            filename = key.split("/")[-1].split("?")[0]
            
        try:
            # Stream from external URL (Cloudinary)
            client = httpx.AsyncClient()
            req = client.build_request("GET", key)
            response = await client.send(req, stream=True)
            
            return StreamingResponse(
                response.aiter_raw(),
                status_code=response.status_code,
                media_type=response.headers.get('content-type', 'application/pdf'),
                headers={
                    'Content-Disposition': f'attachment; filename="{filename}"',
                    'Access-Control-Expose-Headers': 'Content-Disposition'
                },
                background=client.aclose
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to stream external document: {e}")
            raise HTTPException(status_code=500, detail="Failed to retrieve document")

    # Access control (legacy)
    if key.startswith("private/invoices/") or key.startswith("private/tickets/"):
        async with AsyncSessionLocal() as db:
            auth_header = request.headers.get("Authorization")
            token = None
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header[7:]
            else:
                token = request.query_params.get("token")
                
            credentials = None
            if token:
                from fastapi.security.http import HTTPAuthorizationCredentials
                credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
                
            current_user = await get_current_user_optional(credentials=credentials, db=db)
            
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

    raise HTTPException(status_code=404, detail="Legacy R2 keys are no longer accessible.")

