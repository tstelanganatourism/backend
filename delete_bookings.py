import sys
import os
import asyncio

sys.path.append(os.path.abspath(os.curdir))

from app.db.session import AsyncSessionLocal
from app.models.booking import Booking
from sqlalchemy import delete

async def delete_bookings():
    booking_ids = [
        "TBT_BT_1019", "TBT_BT_1008", "TBT_BT_1009", "TBT_BT_1010",
        "TBT_BT_1005", "TBT_BT_1002", "TBT_BT_1000"
    ]
    async with AsyncSessionLocal() as db:
        result = await db.execute(delete(Booking).where(Booking.public_id.in_(booking_ids)))
        await db.commit()
        print(f"Deleted {result.rowcount} bookings.")

if __name__ == "__main__":
    asyncio.run(delete_bookings())
