import asyncio
from app.db.session import AsyncSessionLocal
from sqlalchemy import text

async def clear_inventory():
    async with AsyncSessionLocal() as session:
        print("--- BEFORE CLEARING ---")
        pkg_cnt = (await session.execute(text("SELECT COUNT(*) FROM package_variant_inventory"))).scalar()
        room_cnt = (await session.execute(text("SELECT COUNT(*) FROM room_slot_inventory"))).scalar()
        trans_cnt = (await session.execute(text("SELECT COUNT(*) FROM package_transport_inventory"))).scalar()
        print(f"Package Variant Inventory: {pkg_cnt}")
        print(f"Room Slot Inventory: {room_cnt}")
        print(f"Package Transport Inventory: {trans_cnt}")

        print("\nDeleting all inventory rows from database...")
        await session.execute(text("DELETE FROM package_variant_inventory"))
        await session.execute(text("DELETE FROM room_slot_inventory"))
        await session.execute(text("DELETE FROM package_transport_inventory"))
        await session.commit()

        print("\n--- AFTER CLEARING ---")
        pkg_cnt_after = (await session.execute(text("SELECT COUNT(*) FROM package_variant_inventory"))).scalar()
        room_cnt_after = (await session.execute(text("SELECT COUNT(*) FROM room_slot_inventory"))).scalar()
        trans_cnt_after = (await session.execute(text("SELECT COUNT(*) FROM package_transport_inventory"))).scalar()
        print(f"Package Variant Inventory: {pkg_cnt_after}")
        print(f"Room Slot Inventory: {room_cnt_after}")
        print(f"Package Transport Inventory: {trans_cnt_after}")
        print("\n[SUCCESS] All package, room, and transport inventory records are now 0 (completely empty).")

if __name__ == "__main__":
    asyncio.run(clear_inventory())
