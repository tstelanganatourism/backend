"""
Script to archive/delete old inventory rows.
Removes records older than 60 days to keep the database small and fast.

Usage:
    cd backend
    .\\venv\\Scripts\\python.exe scripts/archive_inventory.py
"""
import asyncio
import sys
import os
import logging
from datetime import date, timedelta

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete
from app.db.session import AsyncSessionLocal
from app.models.room import RoomSlotInventory
from app.models.package import PackageVariantInventory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ARCHIVE_DAYS = 60

async def archive_inventory():
    cutoff_date = date.today() - timedelta(days=ARCHIVE_DAYS)
    logger.info(f"Starting inventory archival. Removing records before {cutoff_date}...")

    async with AsyncSessionLocal() as db:
        try:
            # Delete old RoomSlotInventory
            room_stmt = delete(RoomSlotInventory).where(RoomSlotInventory.date < cutoff_date)
            room_result = await db.execute(room_stmt)
            
            # Delete old PackageVariantInventory
            package_stmt = delete(PackageVariantInventory).where(PackageVariantInventory.date < cutoff_date)
            package_result = await db.execute(package_stmt)
            
            await db.commit()
            
            logger.info(f"Archival complete.")
            logger.info(f"Removed {room_result.rowcount} room slot rows.")
            logger.info(f"Removed {package_result.rowcount} package variant inventory rows.")
            
        except Exception as e:
            await db.rollback()
            logger.error(f"Archival failed: {e}")
            raise

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(archive_inventory())
