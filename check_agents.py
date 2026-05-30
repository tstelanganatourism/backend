import asyncio
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.user import User

async def main():
    session = AsyncSessionLocal()
    try:
        # Fetch all agents
        res = await session.execute(
            select(User).where(User.role.in_(['AGENT', 'ADMIN']))
        )
        users = res.scalars().all()
        print(f"Total Agents/Admins: {len(users)}")
        print(f"{'ID':<5} | {'Email':<30} | {'Name':<25} | {'Role':<10} | {'Comm %':<8} | {'Comm Type':<15}")
        print("-" * 100)
        for u in users:
            print(f"{u.id:<5} | {str(u.email):<30} | {str(u.full_name):<25} | {u.role.value:<10} | {float(u.commission_percentage or 0):<8.2f} | {str(u.commission_type)}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
