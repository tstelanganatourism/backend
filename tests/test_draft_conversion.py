import asyncio
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decimal import Decimal
from datetime import datetime, timezone, timedelta
from app.db.session import AsyncSessionLocal
from app.models.booking import BookingDraft, Booking
from app.models.package import PackageVariantInventory, PackageVariant, Package
from app.api.v1.payments import _finalize_draft

async def run_integration_test():
    async with AsyncSessionLocal() as db:
        # Pre-clean from any previous failed runs
        from sqlalchemy import delete, select
        from app.models.payment import Payment
        from app.models.booking import BookingDraft, Booking
        from app.models.package import PackageVariantInventory, PackageVariant, Package
        
        # Delete payments associated with the mock booking
        await db.execute(Payment.__table__.delete().where(Payment.booking_id.in_(
            select(Booking.id).where(Booking.public_id == 'BK-B2D38482')
        )))
        # Delete bookings
        await db.execute(Booking.__table__.delete().where(Booking.public_id == 'BK-B2D38482'))
        # Delete drafts
        await db.execute(BookingDraft.__table__.delete().where(BookingDraft.draft_id == 'DRF-TEST-123'))
        # Delete variant inventory
        await db.execute(PackageVariantInventory.__table__.delete().where(PackageVariantInventory.variant_id.in_(
            select(PackageVariant.id).where(PackageVariant.package_id.in_(
                select(Package.id).where(Package.slug == 'test-pkg-mock')
            ))
        )))
        # Delete variants
        await db.execute(PackageVariant.__table__.delete().where(PackageVariant.package_id.in_(
            select(Package.id).where(Package.slug == 'test-pkg-mock')
        )))
        # Delete packages
        await db.execute(delete(Package).where(Package.slug == 'test-pkg-mock'))
        await db.commit()

        print("Creating mock draft and inventory...")
        # 1. Ensure a Package & Variant exist
        pkg = Package(title="Test Pkg", type="TOUR", slug="test-pkg-mock")
        db.add(pkg)
        await db.flush()

        variant = PackageVariant(package_id=pkg.id, title="Standard", adult_price=100, child_price=50)
        db.add(variant)
        await db.flush()

        from datetime import date
        today = date.today()
        
        # 2. Add Inventory
        inv = PackageVariantInventory(
            variant_id=variant.id, 
            date=today, 
            total_capacity=50, 
            booked_count=0, 
            reserved_count=5 # Starting with 5 reserved from our draft
        )
        db.add(inv)
        await db.flush()

        # 3. Create Draft
        draft = BookingDraft(
            draft_id="DRF-TEST-123",
            razorpay_order_id="order_test_123",
            checkout_payload={"travel_date": today.isoformat(), "adult_count": 2, "child_count": 0, "passengers": [{"full_name": "Test User", "age": 30, "aadhaar": "123456789012", "phone": "9999999999"}]},
            pricing_snapshot={"subtotal_amount": "200", "coupon_discount": "0", "gst_amount": "10", "gateway_fee": "2", "tourist_total": "212", "agent_discount": "0", "agent_payable": "212"},
            target_type="package",
            variant_id=variant.id,
            travel_date=today,
            quantity=5, # 5 passengers
            amount_payable=212,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)
        )
        db.add(draft)
        await db.commit()

        # 4. Finalize Draft
        print("Finalizing Draft...")
        public_id = await _finalize_draft(draft, "pay_test_456", db)
        await db.commit()
        
        print(f"Draft finalized into Booking: {public_id}")

        # 5. Verify results
        await db.refresh(inv)
        assert inv.reserved_count == 0, f"Expected 0 reserved, got {inv.reserved_count}"
        assert inv.booked_count == 5, f"Expected 5 booked, got {inv.booked_count}"
        print("Inventory Verification PASSED")
        
        # Cleanup
        from app.models.payment import Payment
        await db.execute(Payment.__table__.delete().where(Payment.booking_id.in_(
            select(Booking.id).where(Booking.public_id == public_id)
        )))
        await db.execute(Booking.__table__.delete().where(Booking.public_id == public_id))
        await db.execute(PackageVariantInventory.__table__.delete().where(PackageVariantInventory.id == inv.id))
        await db.execute(PackageVariant.__table__.delete().where(PackageVariant.id == variant.id))
        await db.execute(Package.__table__.delete().where(Package.id == pkg.id))
        await db.commit()
        print("Cleanup done.")

if __name__ == "__main__":
    asyncio.run(run_integration_test())
