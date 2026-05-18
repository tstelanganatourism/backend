import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db.session import engine
from sqlalchemy import text

async def drop_types():
    async with engine.begin() as conn:
        await conn.execute(text("DROP TYPE IF EXISTS promotiontype CASCADE"))
        await conn.execute(text("DROP TYPE IF EXISTS promotiontarget CASCADE"))
        await conn.execute(text("DROP TYPE IF EXISTS promotionbadge CASCADE"))
    print("Types dropped successfully.")

if __name__ == "__main__":
    asyncio.run(drop_types())
