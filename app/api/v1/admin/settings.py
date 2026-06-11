from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any

from app.db.session import get_db
from app.models.settings import SystemSettings
from app.middleware.auth import require_admin
from app.utils.audit import log_action
from app.models.user import User

router = APIRouter(
    prefix="/settings",
    tags=["Admin - Settings"],
    dependencies=[Depends(require_admin)]
)

# Pydantic Schemas for validation
class SystemSettingsUpdateDTO(BaseModel):
    company_name: Optional[str] = None
    support_email: Optional[str] = None
    whatsapp_number: Optional[str] = None
    address: Optional[str] = None
    gst_number: Optional[str] = None
    global_tax_percentage: Optional[int] = None
    cashfree_app_id: Optional[str] = None
    booking_rules: Optional[str] = None
    cancellation_policies: Optional[str] = None
    social_links: Optional[Dict[str, Any]] = None
    default_meta_title: Optional[str] = None
    default_meta_description: Optional[str] = None
    extra_config: Optional[Dict[str, Any]] = None
    
    model_config = ConfigDict(from_attributes=True)

@router.get("", response_model=SystemSettingsUpdateDTO)
async def get_settings(db: AsyncSession = Depends(get_db)):
    """Retrieve the global system settings. Returns an empty object if none exist."""
    result = await db.execute(select(SystemSettings).limit(1))
    settings_record = result.scalar_one_or_none()
    
    if not settings_record:
        return SystemSettingsUpdateDTO() # Return empty if not initialized
    
    return settings_record

@router.put("", response_model=SystemSettingsUpdateDTO)
async def update_settings(
    body: SystemSettingsUpdateDTO,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Update or initialize global system settings."""
    result = await db.execute(select(SystemSettings).limit(1))
    settings_record = result.scalar_one_or_none()
    
    if not settings_record:
        # Create
        settings_record = SystemSettings(**body.model_dump(exclude_unset=True))
        db.add(settings_record)
    else:
        # Update
        update_data = body.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(settings_record, key, value)
            
    await db.commit()
    await db.refresh(settings_record)
    
    # Audit Logging
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="UPDATE_SETTINGS",
        entity_type="SystemSettings",
        entity_id=str(settings_record.id),
        details=body.model_dump(exclude_unset=True)
    )
    await db.commit() # Commit the log
    
    return settings_record
