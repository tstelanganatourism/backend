import asyncio
import os
import sys

# Add backend directory to sys.path so we can import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from app.models.room import Room, RoomHighlight, RoomFAQ, RoomPolicy
from app.models.enums import PolicyType

from sqlalchemy.orm import selectinload

DATABASE_URL = "postgresql+asyncpg://neondb_owner:npg_pr3dCmWeuV0O@ep-dark-credit-aoskzmbc.c-2.ap-southeast-1.aws.neon.tech/neondb"

async def main():
    engine = create_async_engine(DATABASE_URL, echo=True)
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with async_session() as session:
        async with session.begin():
            # Find Room with slug 'godavari-haritha-resort-1'
            stmt = select(Room).where(Room.slug == 'godavari-haritha-resort-1').options(
                selectinload(Room.highlights),
                selectinload(Room.faqs),
                selectinload(Room.policies)
            )
            result = await session.execute(stmt)
            room = result.scalars().first()
            if not room:
                print("Room 'godavari-haritha-resort-1' not found!")
                return

            print(f"Found room: {room.lodge_name} (ID: {room.id})")

            # Clear existing highlights, faqs, policies
            room.highlights = []
            room.faqs = []
            room.policies = []
            room.booking_slots = [
                {"title": "Standard Stay", "slot_start": "12:00:00", "slot_end": "11:00:00"},
                {"title": "Day Use Stay (9 AM - 6 PM)", "slot_start": "09:00:00", "slot_end": "18:00:00"},
                {"title": "Early Check-in (6 AM - 5 AM)", "slot_start": "06:00:00", "slot_end": "05:00:00"}
            ]
            await session.flush()

            # Add highlights
            highlights = [
                RoomHighlight(room_id=room.id, title="Scenic Riverfront Views", icon="Compass", sort_order=1),
                RoomHighlight(room_id=room.id, title="Complimentary High-speed Wi-Fi", icon="Wifi", sort_order=2),
                RoomHighlight(room_id=room.id, title="In-House Fine Dining Restaurant", icon="Utensils", sort_order=3),
                RoomHighlight(room_id=room.id, title="24/7 Security & Power Backup", icon="ShieldCheck", sort_order=4),
            ]
            room.highlights.extend(highlights)

            # Add FAQs
            faqs = [
                RoomFAQ(room_id=room.id, question="What are the check-in and check-out times?", answer="Standard check-in time is 12:00 PM and check-out time is 11:00 AM. Early check-in or late check-out is subject to room availability and additional charges.", sort_order=1),
                RoomFAQ(room_id=room.id, question="Are meals included in the room price?", answer="Meals are not included by default in the room tariff. However, you can order from our in-house multi-cuisine restaurant at your convenience.", sort_order=2),
                RoomFAQ(room_id=room.id, question="Is parking available at the property?", answer="Yes, we provide secure, spacious complimentary parking space for cars and buses on-site.", sort_order=3),
            ]
            room.faqs.extend(faqs)

            # Add Policies
            policies = [
                RoomPolicy(room_id=room.id, type=PolicyType.CANCELLATION, title="Cancellation Policy", description="Free cancellation up to 48 hours prior to the check-in date. Cancellations made within 48 hours will incur a charge equal to 100% of the first night stay.", sort_order=1),
                RoomPolicy(room_id=room.id, type=PolicyType.GENERAL, title="Refund Policy", description="Approved refunds will be processed within 5-7 business days to the original mode of payment.", sort_order=2),
                RoomPolicy(room_id=room.id, type=PolicyType.STAY_RULES, title="Child & Extra Bed Rules", description="Children under 5 years can stay for free sharing existing bedding. Extra mattresses can be provided on request for ₹500 per night.", sort_order=3),
            ]
            room.policies.extend(policies)

            print("Seeded highlights, faqs, policies successfully!")

if __name__ == "__main__":
    asyncio.run(main())
