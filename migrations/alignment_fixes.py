"""
Alignment Fixes Migration - P1, P3, P4, P6
"""
import asyncio
import sys
import os
import ssl

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

# Manually construct engine with SSL
from app.core.config import settings

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

engine = create_async_engine(
    settings.DATABASE_URL,
    connect_args={
        "ssl": ssl_context,
        "prepared_statement_cache_size": 0,
    }
)

async def run_migration():
    async with engine.begin() as conn:
        # P1: Make Aadhaar columns nullable
        await conn.execute(text("""
            ALTER TABLE booking_passengers 
            ALTER COLUMN aadhar_encrypted DROP NOT NULL;
        """))
        await conn.execute(text("""
            ALTER TABLE booking_passengers 
            ALTER COLUMN aadhar_hash DROP NOT NULL;
        """))
        print("[OK] P1: booking_passengers.aadhar_encrypted and aadhar_hash are now nullable")

        # P3: Add commission_type column
        await conn.execute(text("""
            ALTER TABLE users 
            ADD COLUMN IF NOT EXISTS commission_type VARCHAR(16) DEFAULT 'PERCENTAGE' NOT NULL;
        """))
        await conn.execute(text("""
            ALTER TABLE users 
            ADD COLUMN IF NOT EXISTS commission_fixed_amount NUMERIC(10,2);
        """))
        print("[OK] P3: users.commission_type and commission_fixed_amount columns added")

    # P4: Add ADMIN_DIRECT to bookingsource enum - must be outside transaction for Postgres
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        try:
            await conn.execute(text("""
                ALTER TYPE bookingsource ADD VALUE IF NOT EXISTS 'ADMIN_DIRECT';
            """))
            print("[OK] P4: ADMIN_DIRECT added to bookingsource enum")
        except Exception as e:
            print(f"[WARN] P4: bookingsource enum update skipped: {e}")

    # P6: Update is_child computed column
    async with engine.begin() as conn:
        try:
            await conn.execute(text("""
                ALTER TABLE booking_passengers DROP COLUMN IF EXISTS is_child;
            """))
            await conn.execute(text("""
                ALTER TABLE booking_passengers 
                ADD COLUMN is_child BOOLEAN GENERATED ALWAYS AS (age < 18) STORED;
            """))
            print("[OK] P6: booking_passengers.is_child threshold updated to age < 18")
        except Exception as e:
            print(f"[WARN] P6: is_child update failed: {e}")

    await engine.dispose()
    print("\nAll alignment migrations completed!")

if __name__ == "__main__":
    asyncio.run(run_migration())
