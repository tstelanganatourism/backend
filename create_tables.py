import asyncio
from app.db.session import engine
from app.models import Base

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("All tables including checkout_funnel_logs created successfully!")

if __name__ == "__main__":
    asyncio.run(main())
