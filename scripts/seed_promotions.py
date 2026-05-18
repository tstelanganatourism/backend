import asyncio
import sys
import os
from datetime import timedelta

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import AsyncSessionLocal
from app.models.promotion import Promotion
from app.models.enums import PromotionType, PromotionTarget, PromotionBadge
from app.core.timezone import get_ist_now

async def seed_promotions():
    now = get_ist_now()
    valid_until = now + timedelta(days=30)

    promotions_data = [
        {
            "title": "₹1,000 Early Bird Discount!",
            "subtitle": "On all Godavari River Cruises booked 15 days in advance.",
            "icon_emoji": "🛥️",
            "badge": PromotionBadge.NEW_OFFER,
            "type": PromotionType.FLAT_DISCOUNT,
            "target": PromotionTarget.TOURS_ONLY,
            "discount_value": 1000.0,
            "cta_label": "View Tours",
            "cta_url": "/tours",
            "bg_gradient": "from-[#1a6b7a] to-[#0f3d56]",
            "is_active": True,
            "valid_from": now,
            "valid_until": valid_until,
            "sort_order": 10
        },
        {
            "title": "SUMMER 2026: 20% OFF",
            "subtitle": "Special rates for Bhadrachalam Temple Stays & Rooms.",
            "icon_emoji": "☀️",
            "badge": PromotionBadge.SUMMER_SPECIAL,
            "type": PromotionType.PERCENT_DISCOUNT,
            "target": PromotionTarget.ROOMS_ONLY,
            "discount_value": 20.0,
            "cta_label": "Book Rooms",
            "cta_url": "/accommodation",
            "bg_gradient": "from-orange-600 to-red-700",
            "is_active": True,
            "valid_from": now,
            "valid_until": valid_until,
            "sort_order": 20
        },
        {
            "title": "Exclusive Agent Discounts",
            "subtitle": "Partner with TS Tourism for special group booking rates.",
            "icon_emoji": "🤝",
            "badge": PromotionBadge.BESTSELLER,
            "type": PromotionType.INFORMATIONAL,
            "target": PromotionTarget.ALL,
            "cta_label": "Become Agent",
            "cta_url": "/auth/agent/signup",
            "bg_gradient": "from-indigo-600 to-purple-800",
            "is_active": True,
            "valid_from": now,
            "valid_until": valid_until,
            "sort_order": 30
        }
    ]

    async with AsyncSessionLocal() as db:
        print("Seeding promotions...")
        for p_data in promotions_data:
            promo = Promotion(**p_data)
            db.add(promo)
        
        await db.commit()
        print("[OK] Successfully seeded 3 promotions!")

if __name__ == "__main__":
    asyncio.run(seed_promotions())
