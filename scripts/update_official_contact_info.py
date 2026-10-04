import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath('.'))
sys.stdout.reconfigure(encoding='utf-8')
from app.db.session import AsyncSessionLocal
from sqlalchemy import text

OFFICIAL_ADDRESS = 'Door No. 10-1-2/1, Ground Floor, Om Shanthi Building Sataram, Kalyana Mandapam Road, near SBI ATM, Bhadrachalam, Telangana 507111'
OFFICIAL_CONTACTS = '+91 99513 69573, +91 77801 19268'

async def update_data():
    async with AsyncSessionLocal() as session:
        print("=== 1. Updating package_boarding_points ===")
        # Update all Bhadrachalam boarding points with the old address
        bp_update_res = await session.execute(text("""
            UPDATE package_boarding_points
            SET address = :addr,
                contact_number = :contacts
            WHERE address ILIKE '%4-1-78%' 
               OR address ILIKE '%bhavya%' 
               OR address ILIKE '%seetarama%'
               OR contact_number ILIKE '%95420%'
               OR contact_number ILIKE '%98498%'
        """), {
            'addr': OFFICIAL_ADDRESS,
            'contacts': OFFICIAL_CONTACTS
        })
        print(f"Updated boarding points matching old criteria: {bp_update_res.rowcount} rows")

        # Specific updates for station / ghat boarding points
        # ID 17: Kothagudem Railway Station
        await session.execute(text("""
            UPDATE package_boarding_points
            SET address = 'Kothagudem Railway Station (Pickup Point), NH 221, Srinagar, Laxmidevipally, Telangana 507101',
                contact_number = :contacts
            WHERE id = 17
        """), {'contacts': OFFICIAL_CONTACTS})

        # ID 18: Rajahmundry Pushkar ghat
        await session.execute(text("""
            UPDATE package_boarding_points
            SET address = 'Pushkar Ghat, Godavari Riverfront, Rajamahendravaram, Andhra Pradesh 533101',
                contact_number = :contacts
            WHERE id = 18
        """), {'contacts': OFFICIAL_CONTACTS})

        # ID 19: Rajahmundry
        await session.execute(text("""
            UPDATE package_boarding_points
            SET address = 'Pushkar Ghat / Godavari Riverfront, Rajamahendravaram, Andhra Pradesh 533101',
                contact_number = :contacts
            WHERE id = 19
        """), {'contacts': OFFICIAL_CONTACTS})

        print("=== 2. Updating packages table ===")
        # Check package ID 54 description
        pkg54 = await session.execute(text("SELECT description FROM packages WHERE id = 54"))
        desc = pkg54.scalar()
        if desc:
            # Replace old numbers & website
            new_desc = desc
            for old_num in ['98498 48938', '98498 48982', '98498 48983', '95420 69573', '9542069573', '9849848938']:
                new_desc = new_desc.replace(old_num, '+91 99513 69573 / +91 77801 19268')
            new_desc = new_desc.replace('http://www.tsboattourism.org', 'https://www.tstelanganatourism.com')
            new_desc = new_desc.replace('www.tsboattourism.org', 'www.tstelanganatourism.com')
            
            # Clean up duplicate phone lines if any
            clean_contact_html = (
                '<p>Enquiries &amp; Bookings</p>'
                '<p>📞 +91 99513 69573</p>'
                '<p>📞 +91 77801 19268</p>'
                '<p>🌐 Website: <a target="_blank" rel="noopener noreferrer nofollow" href="https://www.tstelanganatourism.com">www.tstelanganatourism.com</a></p>'
            )
            # Find and replace the Enquiries block
            import re
            pattern = re.compile(r'<p>Enquiries &amp; Bookings</p>.*?<p>🌐 Website:.*?</p>', re.DOTALL)
            if pattern.search(new_desc):
                new_desc = pattern.sub(clean_contact_html, new_desc)

            await session.execute(text("""
                UPDATE packages
                SET description = :new_desc
                WHERE id = 54
            """), {'new_desc': new_desc})
            print("Updated package 54 description successfully")

        # Commit all changes
        await session.commit()
        print("Database commit completed!")

        # Verify all boarding points now
        bps = await session.execute(text("""
            SELECT id, package_id, title, address, contact_number, departure_time 
            FROM package_boarding_points 
            ORDER BY id
        """))
        print("\n=== VERIFICATION: All Boarding Points ===")
        for r in bps.fetchall():
            print(dict(r._mapping))

if __name__ == '__main__':
    asyncio.run(update_data())
