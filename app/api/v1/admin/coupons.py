from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from typing import List, Optional

from app.db.session import get_db
from app.models.coupon import Coupon
from app.schemas.coupon import CouponCreate, CouponUpdate, CouponResponse
from app.middleware.auth import require_admin
from app.models.user import User
from app.utils.audit import log_action

router = APIRouter(
    prefix="/coupons",
    tags=["Admin - Coupon Engine"],
    dependencies=[Depends(require_admin)]
)

@router.get("", response_model=List[CouponResponse])
async def list_coupons(
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """List all coupons with optional search filtering by code."""
    query = select(Coupon).where(Coupon.deleted_at.is_(None))
    
    if search:
        query = query.where(Coupon.code.ilike(f"%{search}%"))
        
    query = query.order_by(Coupon.created_at.desc())
    
    result = await db.execute(query)
    return result.scalars().all()

@router.get("/{coupon_id}", response_model=CouponResponse)
async def get_coupon(
    coupon_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get single coupon details."""
    query = select(Coupon).where(Coupon.id == coupon_id, Coupon.deleted_at.is_(None))
    result = await db.execute(query)
    coupon = result.scalar_one_or_none()
    
    if not coupon:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Coupon not found"
        )
        
    return coupon

@router.post("", response_model=CouponResponse, status_code=status.HTTP_201_CREATED)
async def create_coupon(
    body: CouponCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Create a new coupon with audit logging. Enforces unique coupon codes."""
    # Enforce uppercase coupon code
    code_upper = body.code.upper().strip()
    
    # Check if coupon code already exists
    existing = await db.execute(select(Coupon).where(Coupon.code == code_upper, Coupon.deleted_at.is_(None)))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Coupon code already exists"
        )
        
    coupon_data = body.model_dump()
    coupon_data["code"] = code_upper
    
    coupon = Coupon(**coupon_data)
    db.add(coupon)
    await db.commit()
    await db.refresh(coupon)
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="CREATE_COUPON",
        entity_type="Coupon",
        entity_id=str(coupon.id),
        details={"code": coupon.code, "discount_type": coupon.discount_type, "discount_value": float(coupon.discount_value)}
    )
    await db.commit()
    
    return coupon

@router.put("/{coupon_id}", response_model=CouponResponse)
async def update_coupon(
    coupon_id: int,
    body: CouponUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Update a coupon with audit logging."""
    result = await db.execute(select(Coupon).where(Coupon.id == coupon_id, Coupon.deleted_at.is_(None)))
    coupon = result.scalar_one_or_none()
    
    if not coupon:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Coupon not found"
        )
        
    update_data = body.model_dump(exclude_unset=True)
    
    if "code" in update_data:
        code_upper = update_data["code"].upper().strip()
        if code_upper != coupon.code:
            existing = await db.execute(select(Coupon).where(Coupon.code == code_upper, Coupon.deleted_at.is_(None)))
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Coupon code already in use"
                )
            update_data["code"] = code_upper
            
    for key, value in update_data.items():
        setattr(coupon, key, value)
        
    await db.commit()
    await db.refresh(coupon)
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="UPDATE_COUPON",
        entity_type="Coupon",
        entity_id=str(coupon.id),
        details={k: float(v) if isinstance(v, float) else v for k, v in update_data.items()}
    )
    await db.commit()
    
    return coupon

@router.delete("/{coupon_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_coupon(
    coupon_id: int,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin)
):
    """Delete a coupon with audit logging."""
    result = await db.execute(select(Coupon).where(Coupon.id == coupon_id, Coupon.deleted_at.is_(None)))
    coupon = result.scalar_one_or_none()
    
    if not coupon:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Coupon not found"
        )
        
    coupon.deleted_at = func.now()
    await db.commit()
    
    await log_action(
        db=db,
        user_id=current_admin.id,
        action="DELETE_COUPON",
        entity_type="Coupon",
        entity_id=str(coupon.id),
        details={"code": coupon.code}
    )
    await db.commit()
    
    return None
