import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import engine
from sqlalchemy import text

async def run_migration():
    async with engine.begin() as conn:
        try:
            # Drop old computed column
            await conn.execute(text("ALTER TABLE booking_passengers DROP COLUMN is_child;"))
            # Add new computed column with < 10
            await conn.execute(text("ALTER TABLE booking_passengers ADD COLUMN is_child BOOLEAN GENERATED ALWAYS AS (age < 10) STORED;"))
            print("[OK] Reverted is_child threshold to age < 10")
        except Exception as e:
            print(f"[ERROR] {e}")

if __name__ == "__main__":
    asyncio.run(run_migration())
