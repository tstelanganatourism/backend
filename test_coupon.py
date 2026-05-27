import unittest
from datetime import datetime, timedelta
from app.models.coupon import Coupon
from app.core.timezone import get_ist_now

class TestCouponValidation(unittest.TestCase):
    def setUp(self):
        # Create a base coupon that is active and valid
        self.coupon = Coupon(
            code="TEST",
            discount_type="FLAT",
            discount_value=100.0,
            is_active=True,
            usage_limit=10,
            usage_count=0,
            valid_from=get_ist_now() - timedelta(days=1),
            valid_until=get_ist_now() + timedelta(days=1),
        )

    def test_no_minimum_set_coupon_behaves_normally(self):
        # min_tickets is None
        self.coupon.min_tickets = None
        
        # Validates fine regardless of ticket count
        self.assertTrue(self.coupon.is_valid(booking_amount=1000.0, ticket_count=1))
        self.assertTrue(self.coupon.is_valid(booking_amount=1000.0, ticket_count=10))

    def test_below_minimum_passengers_coupon_rejected(self):
        self.coupon.min_tickets = 5
        
        # 4 tickets is below minimum of 5
        self.assertFalse(self.coupon.is_valid(booking_amount=1000.0, ticket_count=4))

    def test_exact_minimum_coupon_accepted(self):
        self.coupon.min_tickets = 5
        
        # 5 tickets is exactly the minimum
        self.assertTrue(self.coupon.is_valid(booking_amount=1000.0, ticket_count=5))

    def test_above_minimum_coupon_accepted(self):
        self.coupon.min_tickets = 5
        
        # 6 tickets is above the minimum
        self.assertTrue(self.coupon.is_valid(booking_amount=1000.0, ticket_count=6))

if __name__ == "__main__":
    unittest.main()
