import asyncio
import os
import sys
from decimal import Decimal

# Add the backend directory to python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text, select
from app.core.config import settings
from app.models.package import (
    Package,
    PackageBoardingPoint,
    PackageExclusion,
    PackageFAQ,
    PackageGalleryImage,
    PackageHighlight,
    PackageInclusion,
    PackageItineraryDay,
    PackagePolicy,
    PackageVariant,
    package_tags,
)
from app.models.room import Room, RoomVariant
from app.models.tag import Tag
from app.models.enums import PackageType, RegionType, PolicyType, PublishStatus
from app.db.session import engine

AsyncSessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, autoflush=False
)

COMMON_INCLUSIONS = [
    {"label": "Boat ticket and operator coordination", "icon": "ship", "sort_order": 1},
    {"label": "Basic lunch or refreshments as per selected package", "icon": "utensils", "sort_order": 2},
    {"label": "Life jacket and standard safety briefing", "icon": "shield", "sort_order": 3},
    {"label": "Local support for reporting and boarding", "icon": "phone", "sort_order": 4},
]

COMMON_EXCLUSIONS = [
    {"label": "Personal expenses, extra snacks, and camera charges", "icon": "wallet", "sort_order": 1},
    {"label": "Special darshan tickets, if required", "icon": "ticket", "sort_order": 2},
]

COMMON_FAQS = [
    {
        "question": "Is Aadhaar mandatory for this package?",
        "answer": "Yes. Please carry original Aadhaar or any valid government photo ID. The operator may verify ID details before boarding.",
        "sort_order": 1,
    },
    {
        "question": "Can timings change on the travel date?",
        "answer": "Small timing changes can happen due to traffic, river water level, permissions, or weather. Final reporting instructions are shared before travel.",
        "sort_order": 2,
    },
    {
        "question": "Is food included?",
        "answer": "Food depends on the selected package. Please check the inclusions section and fare variant before booking.",
        "sort_order": 3,
    },
]

COMMON_POLICIES = [
    {
        "type": PolicyType.CHECK_IN_OUT,
        "title": "Government ID and reporting verification",
        "description": "All passengers must carry a valid physical government ID. Aadhaar details or document references may be required during final ticket verification.",
        "sort_order": 1,
    },
    {
        "type": PolicyType.TRAVEL_RULES,
        "title": "Timings confirmed before travel",
        "description": "Reporting point, boarding time, pickup location, and return timing are confirmed by the operator before departure based on date, weather, and official permissions.",
        "sort_order": 2,
    },
    {
        "type": PolicyType.CANCELLATION,
        "title": "Cancellation and refund confirmation",
        "description": "Cancellation terms depend on boat slot blocking, transport arrangement, stay allocation, and operator policy for the selected travel date.",
        "sort_order": 3,
    },
]

def default_boarding(region: RegionType, title: str):
    if "rajahmundry" in title.lower() and region == RegionType.AP:
        return [
            {
                "title": "Rajahmundry Tourism Boat Point",
                "address": "Godavari Bund Road, Rajahmundry, Andhra Pradesh",
                "map_url": "https://maps.google.com/maps?q=Rajahmundry%20Godavari%20Bund%20Boat%20Point&output=embed",
                "departure_time": "07:30 AM",
                "sort_order": 1,
            }
        ]
    return [
        {
            "title": "Bhadrachalam Tourism Office",
            "address": "Near Sri Sita Ramachandra Swamy Temple, Bhadrachalam, Telangana",
            "map_url": "https://maps.google.com/maps?q=Bhadrachalam%20Temple%20Tourism%20Office&output=embed",
            "departure_time": "07:30 AM",
            "sort_order": 1,
        }
    ]

async def ensure_nested(session, package_id, model, rows, identity_field):
    for row in rows:
        existing = await session.execute(
            select(model).where(
                model.package_id == package_id,
                getattr(model, identity_field) == row[identity_field],
            )
        )
        item = existing.scalar_one_or_none()
        if item:
            for key, value in row.items():
                setattr(item, key, value)
        else:
            item = model(package_id=package_id, **row)
        session.add(item)

async def seed_db():
    print("Seeding database with Papikondalu Tourism data...")
    
    async with AsyncSessionLocal() as session:
        # 1. Create or Fetch Tags
        tags_data = ["River Cruise", "Temple", "Nature", "Family Friendly", "A/C Transport", "Meals Included"]
        tags = []
        for name in tags_data:
            existing_tag = await session.execute(select(Tag).where(Tag.name == name))
            tag = existing_tag.scalar_one_or_none()
            if not tag:
                tag = Tag(name=name, is_active=True)
                session.add(tag)
            tags.append(tag)
        
        await session.flush()
        
        # 2. Create Packages (if not exists)
        packages_to_seed = [
            {
                "title": "Hyderabad to Bhadrachalam & Papikondalu (3 Night & 2 Days)",
                "slug": "hyd-bhadrachalam-papikondalu-3n2d",
                "type": PackageType.TOUR,
                "region": RegionType.TS,
                "description": "A comprehensive package covering Hyderabad to Bhadrachalam via bus, followed by a serene Godavari boat cruise to Papikondalu with a night stay at Kolluru Bamboo Huts.",
                "cover_image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912633/telpkg11_jlak8x.jpg",
                "is_featured": True,
                "order_priority": 1,
                "variants": [
                    {"title": "Standard A/C Boat + Bus", "adult_price": Decimal("4500.00"), "child_price": Decimal("3500.00"), "transport_info": "Bus + Boat"}
                ],
                "tag_indices": [0, 2, 5]
            },
            {
                "title": "Bhadrachalam to Papikondalu 1-Day Tour",
                "slug": "bhadrachalam-papikondalu-1-day",
                "type": PackageType.TOUR,
                "region": RegionType.TS,
                "description": "Experience a beautiful 1-day scenic cruise on the Godavari river to the majestic Papikondalu hills starting from Bhadrachalam.",
                "cover_image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912635/telpkg21_qgjrl1.jpg",
                "is_featured": True,
                "order_priority": 2,
                "variants": [
                    {"title": "Day Cruise", "adult_price": Decimal("1200.00"), "child_price": Decimal("1000.00"), "transport_info": "Boat Only"}
                ],
                "tag_indices": [0, 3]
            },
            {
                "title": "Bhadrachalam to Rajahmundry 2-Day Package",
                "slug": "bhadrachalam-rajahmundry-2-day",
                "type": PackageType.TRIP,
                "region": RegionType.AP,
                "description": "A complete 2-day journey starting from Bhadrachalam, cruising through Papikondalu, and dropping at Rajahmundry.",
                "cover_image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912638/telpkg31_l5z0jc.jpg",
                "is_featured": False,
                "order_priority": 3,
                "variants": [
                    {"title": "2-Day River Journey", "adult_price": Decimal("3500.00"), "child_price": Decimal("2800.00"), "transport_info": "Boat + Stay"}
                ],
                "tag_indices": [0, 4]
            },
            {
                "title": "Rajahmundry to Papikondalu (Night Stay)",
                "slug": "rajahmundry-papikondalu-night-stay",
                "type": PackageType.TOUR,
                "region": RegionType.AP,
                "description": "Luxury boat trip from Rajahmundry to Papikondalu with overnight stay in Kolluru Bamboo Huts.",
                "cover_image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912641/telpkg41_mblbra.jpg",
                "is_featured": True,
                "order_priority": 4,
                "variants": [
                    {"title": "Executive AC Boat", "adult_price": Decimal("5000.00"), "child_price": Decimal("4000.00"), "transport_info": "AC Boat"}
                ],
                "tag_indices": [0, 2, 5]
            },
            {
                "title": "Bhadrachalam Temple & Local Sightseeing",
                "slug": "bhadrachalam-local-sightseeing",
                "type": PackageType.TRIP,
                "region": RegionType.TS,
                "description": "Visit the holy Sita Ramachandra Swamy temple and other local attractions around Bhadrachalam.",
                "cover_image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912645/telpkg51_zihj7v.jpg",
                "is_featured": False,
                "order_priority": 5,
                "variants": [
                    {
                        "title": "Self Reporting - No Transport",
                        "adult_price": Decimal("1300.00"),
                        "child_price": Decimal("1100.00"),
                        "transport_info": "No transport included. Customer reports directly at the sightseeing start point."
                    },
                    {
                        "title": "With A/C Transport",
                        "adult_price": Decimal("1800.00"),
                        "child_price": Decimal("1600.00"),
                        "transport_info": "A/C shared transport with local sightseeing coordination."
                    },
                    {
                        "title": "With Non-A/C Transport",
                        "adult_price": Decimal("1600.00"),
                        "child_price": Decimal("1400.00"),
                        "transport_info": "Non-A/C shared transport with local sightseeing coordination."
                    },
                    {
                        "title": "Self Reporting + Refreshments & Bath Access",
                        "adult_price": Decimal("1500.00"),
                        "child_price": Decimal("1300.00"),
                        "transport_info": "No transport. Includes refreshments plus washroom and bathroom/bath access. Rs. 200 per person add-on included."
                    }
                ],
                "tag_indices": [1, 3]
            }
        ]

        for p_data in packages_to_seed:
            existing_p_result = await session.execute(select(Package).where(Package.slug == p_data["slug"]))
            p = existing_p_result.scalar_one_or_none()
            if not p:
                p = Package(
                    title=p_data["title"],
                    slug=p_data["slug"],
                    type=p_data["type"],
                    region=p_data["region"],
                    description=p_data["description"],
                    cover_image_url=p_data["cover_image"],
                    order_priority=p_data["order_priority"],
                    is_featured=p_data["is_featured"],
                    is_active=True,
                    status=PublishStatus.PUBLISHED,
                )
                session.add(p)
                await session.flush()
            else:
                p.title = p_data["title"]
                p.type = p_data["type"]
                p.region = p_data["region"]
                p.description = p_data["description"]
                p.cover_image_url = p_data["cover_image"]
                p.order_priority = p_data["order_priority"]
                p.is_featured = p_data["is_featured"]
                p.is_active = True
                p.status = PublishStatus.PUBLISHED
                session.add(p)
                await session.flush()

            # Attach missing tags safely
            existing_tag_ids_result = await session.execute(
                select(package_tags.c.tag_id).where(package_tags.c.package_id == p.id)
            )
            existing_tag_ids = set(existing_tag_ids_result.scalars().all())
            for idx in p_data["tag_indices"]:
                if tags[idx].id not in existing_tag_ids:
                    await session.execute(
                        package_tags.insert().values(package_id=p.id, tag_id=tags[idx].id)
                    )

            # Ensure paid active variants exist and never stay as zero-priced demo rows
            for v_data in p_data["variants"]:
                existing_v_result = await session.execute(
                    select(PackageVariant).where(
                        PackageVariant.package_id == p.id,
                        PackageVariant.title == v_data["title"],
                    )
                )
                v = existing_v_result.scalar_one_or_none()
                if not v:
                    v = PackageVariant(package_id=p.id, title=v_data["title"])
                v.adult_price = v_data["adult_price"]
                v.child_price = v_data["child_price"]
                v.transport_info = v_data["transport_info"]
                v.is_active = True
                session.add(v)

            if p_data["slug"] == "bhadrachalam-local-sightseeing":
                desired_variant_titles = {variant["title"] for variant in p_data["variants"]}
                old_variants_result = await session.execute(
                    select(PackageVariant).where(PackageVariant.package_id == p.id)
                )
                for old_variant in old_variants_result.scalars().all():
                    if old_variant.title not in desired_variant_titles:
                        old_variant.is_active = False
                        session.add(old_variant)

            itinerary = [
                {
                    "day_number": 1,
                    "title": "Reporting, boarding and sightseeing",
                    "description": "07:30 AM - Report at the boarding point with original ID proof.\n08:30 AM - Operator verification and safety instructions.\n09:00 AM - Journey starts as per selected route.\n01:00 PM - Food or refreshment break as per package inclusion.\n06:00 PM - Return or onward drop coordination.",
                    "icon": "route",
                    "meal_included": True,
                    "sort_order": 1,
                }
            ]
            if "2-Day" in p_data["title"] or "Night" in p_data["title"] or "3 Night" in p_data["title"]:
                itinerary.append(
                    {
                        "day_number": 2,
                        "title": "Return journey and drop",
                        "description": "08:00 AM - Breakfast or morning refreshment.\n10:00 AM - Local sightseeing or river route continuation.\n02:00 PM - Return boat or road transfer starts.\n06:30 PM - Drop at confirmed return point.",
                        "icon": "ship",
                        "meal_included": True,
                        "sort_order": 2,
                    }
                )

            highlights = [
                {"title": "Godavari river route", "icon": "ship", "sort_order": 1},
                {"title": "Papikondalu scenic views", "icon": "mountain", "sort_order": 2},
                {"title": "Family-friendly coordination", "icon": "users", "sort_order": 3},
            ]
            if "Temple" in p_data["title"]:
                highlights = [
                    {"title": "Sri Sita Ramachandra Swamy Temple", "icon": "landmark", "sort_order": 1},
                    {"title": "Parnasala local sightseeing", "icon": "map", "sort_order": 2},
                    {"title": "Private local transport option", "icon": "car", "sort_order": 3},
                ]

            gallery = [
                {
                    "image_url": p_data["cover_image"],
                    "alt_text": p_data["title"],
                    "is_cover": True,
                    "category": "cover",
                    "sort_order": 1,
                }
            ]

            await ensure_nested(session, p.id, PackageGalleryImage, gallery, "image_url")
            await ensure_nested(session, p.id, PackageItineraryDay, itinerary, "title")
            await ensure_nested(session, p.id, PackageHighlight, highlights, "title")
            await ensure_nested(session, p.id, PackageInclusion, COMMON_INCLUSIONS, "label")
            await ensure_nested(session, p.id, PackageExclusion, COMMON_EXCLUSIONS, "label")
            await ensure_nested(session, p.id, PackageBoardingPoint, default_boarding(p_data["region"], p_data["title"]), "title")
            await ensure_nested(session, p.id, PackageFAQ, COMMON_FAQS, "question")
            await ensure_nested(session, p.id, PackagePolicy, COMMON_POLICIES, "title")
        
        # 4. Create Rooms (Lodges)
        rooms_to_seed = [
            {
                "name": "Godavari Haritha Resort",
                "address": "Near Godavari River Bank, Bhadrachalam",
                "facilities": ["River View", "A/C", "TV", "Restaurant", "Garden"],
                "featured": True,
                "priority": 1,
                "image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912666/telpkg1_t9luad.jpg",
                "variants": [
                    {"name": "A/C Double Room", "w_price": Decimal("2500.00"), "we_price": Decimal("3000.00"), "cap": 2},
                    {"name": "Non A/C Double Room", "w_price": Decimal("1800.00"), "we_price": Decimal("2200.00"), "cap": 2}
                ]
            },
            {
                "name": "Ram Dhanush Residency",
                "address": "Near Ramalayam Temple, Bhadrachalam",
                "facilities": ["Room Service", "TV", "Invertor", "Car Parking", "Hot Water"],
                "featured": True,
                "priority": 2,
                "image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912669/telpkg2_c8edkp.jpg",
                "variants": [
                    {"name": "A/C", "w_price": Decimal("1400.00"), "we_price": Decimal("1500.00"), "cap": 4},
                    {"name": "NON A/C", "w_price": Decimal("900.00"), "we_price": Decimal("1000.00"), "cap": 4}
                ]
            },
            {
                "name": "Ravi Sai Residency",
                "address": "Opp. RTC Bus Stand, Bhadrachalam",
                "facilities": ["Room Service", "TV", "Car Parking"],
                "featured": False,
                "priority": 3,
                "image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912672/telpkg10_ciqxx2.jpg",
                "variants": [
                    {"name": "Deluxe AC", "w_price": Decimal("1800.00"), "we_price": Decimal("2200.00"), "cap": 2},
                    {"name": "Standard Non-AC", "w_price": Decimal("1000.00"), "we_price": Decimal("1200.00"), "cap": 3}
                ]
            },
            {
                "name": "Uma Maheswara Residency",
                "address": "Beside Temple Parking, Bhadrachalam",
                "facilities": ["TV", "Hot Water"],
                "featured": True,
                "priority": 4,
                "image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912675/telpkg3_judhqc.jpg",
                "variants": [
                    {"name": "A/C Double", "w_price": Decimal("1600.00"), "we_price": Decimal("1800.00"), "cap": 2}
                ]
            },
            {
                "name": "Sri Seetha Rama Residency",
                "address": "Opp: Godavari River, Bhadrachalam",
                "facilities": ["River View", "A/C", "Elevator"],
                "featured": False,
                "priority": 5,
                "image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912677/telpkg40_bzlqjp.jpg",
                "variants": [
                    {"name": "River View AC", "w_price": Decimal("2200.00"), "we_price": Decimal("2500.00"), "cap": 2}
                ]
            },
            {
                "name": "Hotel Bhadrachalam",
                "address": "Main Road, Bhadrachalam",
                "facilities": ["Parking", "A/C", "Restaurant"],
                "featured": True,
                "priority": 6,
                "image": "https://res.cloudinary.com/dpdab3e97/image/upload/q_auto/f_auto/v1778912687/telpkg15_bodz0e.jpg",
                "variants": [
                    {"name": "Family Suite", "w_price": Decimal("3500.00"), "we_price": Decimal("4000.00"), "cap": 4}
                ]
            }
        ]

        for r_data in rooms_to_seed:
            existing_r_res = await session.execute(select(Room).where(Room.lodge_name == r_data["name"]))
            r = existing_r_res.scalar_one_or_none()
            
            if not r:
                r = Room(
                    lodge_name=r_data["name"],
                    address=r_data["address"],
                    facilities=r_data["facilities"],
                    total_rooms=20,
                    slot_start=__import__('datetime').time(12, 0),
                    slot_end=__import__('datetime').time(11, 0),
                    cover_image_url=r_data["image"],
                    description=f"Welcome to {r_data['name']}, providing quality stay in Bhadrachalam.",
                    is_featured=r_data["featured"],
                    order_priority=r_data["priority"],
                    is_active=True
                )
                session.add(r)
                await session.flush()
            else:
                # Update existing room details
                r.cover_image_url = r_data["image"]
                r.is_featured = r_data["featured"]
                r.order_priority = r_data["priority"]
                r.address = r_data["address"]
                r.facilities = r_data["facilities"]
                session.add(r)
                await session.flush()

            # Ensure variants exist for this room
            for v_data in r_data["variants"]:
                existing_v_res = await session.execute(
                    select(RoomVariant).where(
                        RoomVariant.room_id == r.id, 
                        RoomVariant.variant_name == v_data["name"]
                    )
                )
                v = existing_v_res.scalar_one_or_none()
                if not v:
                    v = RoomVariant(
                        room_id=r.id,
                        variant_name=v_data["name"],
                        weekday_price=v_data["w_price"],
                        weekend_price=v_data["we_price"],
                        capacity_per_room=v_data["cap"],
                        is_active=True
                    )
                    session.add(v)
                else:
                    # Update existing variant prices
                    v.weekday_price = v_data["w_price"]
                    v.weekend_price = v_data["we_price"]
                    session.add(v)
        
        await session.commit()
        print("Successfully seeded data.")

if __name__ == "__main__":
    asyncio.run(seed_db())
