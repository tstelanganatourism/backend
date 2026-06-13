import sys
import os
import asyncio

sys.path.append(os.path.abspath(os.curdir))

from app.db.session import AsyncSessionLocal
from app.models.user import User
from app.models.enums import UserRole, AccountStatus
from app.core.security import get_password_hash
from sqlalchemy import select

async def create_admin():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).filter(User.email == "admin@example.com"))
        existing_admin = result.scalar_one_or_none()
        if not existing_admin:
            admin = User(
                email="admin@example.com",
                password_hash=get_password_hash("admin123"),
                full_name="Test Admin",
                role=UserRole.ADMIN,
                account_status=AccountStatus.ACTIVE,
                is_active=True
            )
            db.add(admin)
            await db.commit()
            print("Admin user created: admin@example.com / admin123")
        else:
            existing_admin.password_hash = get_password_hash("admin123")
            await db.commit()
            print("Admin user already exists. Password updated to admin123")

if __name__ == "__main__":
    asyncio.run(create_admin())