"""
One-time admin seed script.
Creates the first super-admin account.

Usage:
    cd backend
    .\\venv\\Scripts\\python.exe scripts/seed_admin.py

IMPORTANT:
  - Change ADMIN_EMAIL and ADMIN_PASSWORD before running in production.
  - Admin self-registration is permanently disabled by the auth router.
  - Run this script ONCE only.
"""
import asyncio
import sys
import os

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import AsyncSessionLocal
from app.models.user import User
from app.models.enums import UserRole, AccountStatus
from app.core.security import get_password_hash
from sqlalchemy import select

# ─── CONFIGURE THESE BEFORE RUNNING ──────────────────────────────────────────
ADMIN_FULL_NAME = "Super Admin"
ADMIN_EMAIL     = "tsboattourismservices@gmail.com"
ADMIN_PASSWORD  = "tstourism@2006"   # CHANGE THIS in production
# ─────────────────────────────────────────────────────────────────────────────


async def seed_admin():
    async with AsyncSessionLocal() as db:
        # Remove old dummy placeholder if it exists
        old_dummy_email = "admin@papikondalutourism.com"
        from sqlalchemy import delete
        await db.execute(delete(User).where(User.email == old_dummy_email))
        await db.commit()

        # Check if new admin already exists
        result = await db.execute(
            select(User).where(User.email == ADMIN_EMAIL)
        )
        existing = result.scalar_one_or_none()

        if existing:
            print(f"[SKIP] Admin account already exists: {ADMIN_EMAIL}")
            return

        admin = User(
            full_name=ADMIN_FULL_NAME,
            email=ADMIN_EMAIL,
            password_hash=get_password_hash(ADMIN_PASSWORD),
            role=UserRole.ADMIN,
            account_status=AccountStatus.ACTIVE,
            is_active=True,
        )
        db.add(admin)
        await db.commit()
        await db.refresh(admin)

        print(f"[OK] Admin account created successfully!")
        print(f"     ID:    {admin.id}")
        print(f"     Email: {admin.email}")
        print(f"     Role:  {admin.role.value}")
        print(f"\n[IMPORTANT] Change the default password before going to production!")


if __name__ == "__main__":
    asyncio.run(seed_admin())
