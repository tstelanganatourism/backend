import asyncio
import os
import sys
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta, date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import AsyncSessionLocal
from app.models.booking import BookingDraft, Booking, BookingPassenger
from app.models.package import Package, PackageVariant, PackageVariantInventory
from app.models.user import User
from app.models.enums import BookingStatus
from app.api.v1.public_bookings import process_checkout, CheckoutRequest, PassengerInput
from app.api.v1.payments import _finalize_draft
from app.utils.pricing import get_effective_package_prices
from app.utils.verhoeff import is_valid_aadhaar
from app.services.razorpay_client import razorpay_service
from sqlalchemy import delete, select
from fastapi import HTTPException

def get_valid_aadhaars():
    """Generates two valid Aadhaar strings that pass Verhoeff and prefix rules."""
    valid_list = []
    # Prefix must not start with 0 or 1
    for prefix in ["36666666666", "46666666666"]:
        for d in range(10):
            candidate = prefix + str(d)
            if is_valid_aadhaar(candidate):
                valid_list.append(candidate)
                break
    return valid_list

@pytest.mark.asyncio
async def test_override_pricing():
    # Force mock client on Razorpay service to avoid external API calls
    razorpay_service.client = None

    valid_aadhaars = get_valid_aadhaars()
    assert len(valid_aadhaars) >= 2, "Failed to generate valid test Aadhaar numbers"
    aadhaar_1, aadhaar_2 = valid_aadhaars[0], valid_aadhaars[1]
    print(f"Generated valid Aadhaars for test: {aadhaar_1}, {aadhaar_2}")

    pkg = None
    variant = None
    inventory = None
    run_timestamp = int(datetime.now().timestamp())

    async with AsyncSessionLocal() as db:
        try:
            print("=== STAGED SETUP ===")
            # 1. Create a package and a variant
            pkg = Package(
                title="Pricing Override Test Package",
                type="TOUR",
                slug=f"override-test-pkg-{run_timestamp}"
            )
            db.add(pkg)
            await db.flush()

            variant = PackageVariant(
                package_id=pkg.id,
                title="Test Variant",
                adult_price=Decimal("1000.00"),
                child_price=Decimal("500.00")
            )
            db.add(variant)
            await db.flush()

            travel_date = date.today() + timedelta(days=2) # Future date
            
            # 2. Setup inventory row with no override first
            inventory = PackageVariantInventory(
                variant_id=variant.id,
                date=travel_date,
                total_capacity=10,
                booked_count=0,
                reserved_count=0,
                is_closed=False,
                price_override=None
            )
            db.add(inventory)
            await db.commit()

            print(f"Created package ID: {pkg.id}, variant ID: {variant.id}, date: {travel_date}")

            # -------------------------------------------------------------
            # Test Case 1: Positive Override Application
            # -------------------------------------------------------------
            print("\nRunning Test Case 1: Positive override (+200.00)")
            inventory.price_override = Decimal("200.00")
            await db.commit()
            
            # Helper verification
            eff_adult, eff_child = get_effective_package_prices(
                variant.adult_price, variant.child_price, inventory.price_override
            )
            assert eff_adult == Decimal("1200.00"), f"Expected 1200, got {eff_adult}"
            assert eff_child == Decimal("700.00"), f"Expected 700, got {eff_child}"
            print("  - Shared helper pricing calculation verification: PASSED")

            # Checkout calculation verification
            req1 = CheckoutRequest(
                target_type="package",
                travel_date=travel_date,
                quantity=2,
                variant_id=variant.id,
                adult_count=2,
                child_count=0,
                passengers=[
                    PassengerInput(full_name="Passenger One", age=30, phone="9999999999", aadhaar=aadhaar_1),
                    PassengerInput(full_name="Passenger Two", age=28, phone="8888888888", aadhaar=aadhaar_2)
                ]
            )
            res1 = await process_checkout(request=req1, db=db, current_user=None)
            draft1_id = res1["checkout_data"]["draft_id"]
            
            # Fetch draft to inspect snapshot
            draft1 = (await db.execute(select(BookingDraft).where(BookingDraft.draft_id == draft1_id))).scalar_one()
            assert Decimal(draft1.pricing_snapshot["subtotal_amount"]) == Decimal("2400.00"), \
                f"Expected subtotal 2400, got {draft1.pricing_snapshot['subtotal_amount']}"
            print("  - Checkout price and draft snapshot verification: PASSED")

            # Clean draft 1
            await db.delete(draft1)
            inventory.reserved_count = 0
            await db.commit()

            # -------------------------------------------------------------
            # Test Case 2: Negative Override Application & Clamping to 0
            # -------------------------------------------------------------
            print("\nRunning Test Case 2: Negative override (-600.00) & clamping (-1200.00)")
            inventory.price_override = Decimal("-600.00")
            await db.commit()
            
            eff_adult, eff_child = get_effective_package_prices(
                variant.adult_price, variant.child_price, inventory.price_override
            )
            assert eff_adult == Decimal("400.00"), f"Expected 400, got {eff_adult}"
            assert eff_child == Decimal("0.00"), f"Expected 0, got {eff_child}"

            # Clamping check (-1200.00 override)
            inventory.price_override = Decimal("-1200.00")
            await db.commit()
            eff_adult, eff_child = get_effective_package_prices(
                variant.adult_price, variant.child_price, inventory.price_override
            )
            assert eff_adult == Decimal("0.00"), f"Expected clamp to 0.00, got {eff_adult}"
            assert eff_child == Decimal("0.00"), f"Expected clamp to 0.00, got {eff_child}"

            # Checkout with negative override
            inventory.price_override = Decimal("-600.00")
            await db.commit()

            req2 = CheckoutRequest(
                target_type="package",
                travel_date=travel_date,
                quantity=2,
                variant_id=variant.id,
                adult_count=1,
                child_count=1,
                passengers=[
                    PassengerInput(full_name="Adult Pass", age=35, phone="9999999999", aadhaar=aadhaar_1),
                    PassengerInput(full_name="Child Pass", age=7)
                ]
            )
            res2 = await process_checkout(request=req2, db=db, current_user=None)
            draft2_id = res2["checkout_data"]["draft_id"]
            
            draft2 = (await db.execute(select(BookingDraft).where(BookingDraft.draft_id == draft2_id))).scalar_one()
            # Adult: 1000 - 600 = 400. Child: 500 - 600 = -100 -> clamped to 0. Subtotal = 400 + 0 = 400.
            assert Decimal(draft2.pricing_snapshot["subtotal_amount"]) == Decimal("400.00"), \
                f"Expected subtotal 400, got {draft2.pricing_snapshot['subtotal_amount']}"
            print("  - Negative override application and clamp to zero verification: PASSED")

            # Clean draft 2
            await db.delete(draft2)
            inventory.reserved_count = 0
            await db.commit()

            # -------------------------------------------------------------
            # Test Case 3: Closed Date Before Checkout (should block)
            # -------------------------------------------------------------
            print("\nRunning Test Case 3: Closed date before checkout")
            inventory.is_closed = True
            await db.commit()

            blocked = False
            try:
                await process_checkout(request=req1, db=db, current_user=None)
            except HTTPException as e:
                assert e.status_code == 400
                assert "closed" in e.detail.lower()
                blocked = True
            
            assert blocked, "Checkout should have been blocked for closed date but was not"
            print("  - Closed date blocking checkout verification: PASSED")

            # Re-open for draft creation
            inventory.is_closed = False
            await db.commit()

            # -------------------------------------------------------------
            # Test Case 4: Closed Date After Draft Creation (should allow payment/finalization)
            # -------------------------------------------------------------
            print("\nRunning Test Case 4: Closed date after draft creation")
            # Create a draft
            res4 = await process_checkout(request=req1, db=db, current_user=None)
            draft4_id = res4["checkout_data"]["draft_id"]
            draft4 = (await db.execute(select(BookingDraft).where(BookingDraft.draft_id == draft4_id))).scalar_one()

            # Now, simulate admin closing the date after draft creation
            inventory.is_closed = True
            await db.commit()

            # Webhook/Finalization should still process this draft successfully
            payment_id_4 = f"pay_mock_test_4_{run_timestamp}"
            public_id = await _finalize_draft(draft4, payment_id_4, db)
            await db.commit()

            # Verify booking exists
            booking = (await db.execute(select(Booking).where(Booking.public_id == public_id))).scalar_one()
            assert booking.status == BookingStatus.CONFIRMED, f"Expected BookingStatus.CONFIRMED, got {booking.status}"
            print("  - Webhook/Finalization on closed date with existing draft verification: PASSED")

            # Cleanup booking 4 and draft 4
            await db.execute(delete(BookingPassenger).where(BookingPassenger.booking_id == booking.id))
            from app.models.payment import Payment
            await db.execute(delete(Payment).where(Payment.booking_id == booking.id))
            await db.execute(delete(Booking).where(Booking.id == booking.id))
            
            existing_draft = (await db.execute(select(BookingDraft).where(BookingDraft.draft_id == draft4_id))).scalar_one_or_none()
            if existing_draft:
                await db.delete(existing_draft)
            inventory.reserved_count = 0
            inventory.booked_count = 0
            inventory.is_closed = False
            await db.commit()

            # -------------------------------------------------------------
            # Test Case 5: Snapshot Consistency Between Checkout and Finalization
            # -------------------------------------------------------------
            print("\nRunning Test Case 5: Snapshot consistency check")
            inventory.price_override = Decimal("-200.00")
            await db.commit()

            res5 = await process_checkout(request=req1, db=db, current_user=None)
            draft5_id = res5["checkout_data"]["draft_id"]
            draft5 = (await db.execute(select(BookingDraft).where(BookingDraft.draft_id == draft5_id))).scalar_one()

            # Capture snapshot from draft
            snap = draft5.pricing_snapshot
            expected_subtotal = Decimal(snap["subtotal_amount"])
            expected_gst = Decimal(snap["gst_amount"])
            expected_total = Decimal(snap["tourist_total"])
            expected_payable = Decimal(snap["agent_payable"])

            # Finalize
            payment_id_5 = f"pay_mock_test_5_{run_timestamp}"
            pub_id5 = await _finalize_draft(draft5, payment_id_5, db)
            await db.commit()

            # Verify booking values match exactly
            booking5 = (await db.execute(select(Booking).where(Booking.public_id == pub_id5))).scalar_one()
            assert booking5.subtotal_amount == expected_subtotal, f"Expected {expected_subtotal}, got {booking5.subtotal_amount}"
            assert booking5.gst_amount == expected_gst, f"Expected {expected_gst}, got {booking5.gst_amount}"
            assert booking5.total_amount == expected_total, f"Expected {expected_total}, got {booking5.total_amount}"
            assert booking5.paid_amount == expected_payable, f"Expected {expected_payable}, got {booking5.paid_amount}"
            assert booking5.pricing_snapshot == snap, f"Expected snapshot {snap}, got {booking5.pricing_snapshot}"
            print("  - Snapshot consistency verification: PASSED")

            # Cleanup booking 5 and draft 5
            await db.execute(delete(BookingPassenger).where(BookingPassenger.booking_id == booking5.id))
            await db.execute(delete(Payment).where(Payment.booking_id == booking5.id))
            await db.execute(delete(Booking).where(Booking.id == booking5.id))
            existing_draft5 = (await db.execute(select(BookingDraft).where(BookingDraft.draft_id == draft5_id))).scalar_one_or_none()
            if existing_draft5:
                await db.delete(existing_draft5)
            inventory.reserved_count = 0
            inventory.booked_count = 0
            await db.commit()

            print("\nALL INTEGRATION TEST CASES PASSED SUCCESSFULLY!")

        except Exception as e:
            print(f"\nTEST FAILED WITH EXCEPTION: {e}")
            raise e

        finally:
            # Clear pending rollback states if there was a DB flush failure
            await db.rollback()
            
            print("\nCleaning up staging infrastructure...")
            if variant is not None:
                # Find all booking IDs that reference this variant just in case some weren't cleaned up due to failure
                booking_ids_query = await db.execute(select(Booking.id).where(Booking.variant_id == variant.id))
                booking_ids = [row[0] for row in booking_ids_query.all()]
                if booking_ids:
                    from app.models.payment import Payment
                    await db.execute(delete(Payment).where(Payment.booking_id.in_(booking_ids)))
                    await db.execute(delete(BookingPassenger).where(BookingPassenger.booking_id.in_(booking_ids)))
                    await db.execute(delete(Booking).where(Booking.id.in_(booking_ids)))
                
                await db.execute(delete(BookingDraft).where(BookingDraft.variant_id == variant.id))
            
            if inventory is not None:
                await db.execute(delete(PackageVariantInventory).where(PackageVariantInventory.id == inventory.id))
            if variant is not None:
                await db.execute(delete(PackageVariant).where(PackageVariant.id == variant.id))
            if pkg is not None:
                await db.execute(delete(Package).where(Package.id == pkg.id))
            
            await db.commit()
            print("Cleanup completed.")

if __name__ == "__main__":
    asyncio.run(test_override_pricing())
