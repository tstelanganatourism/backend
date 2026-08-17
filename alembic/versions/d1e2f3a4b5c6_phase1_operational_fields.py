"""phase1_operational_fields

Add operational tourism fields to boarding points, itinerary days, gallery images,
and packages. Expand PolicyType enum with real tourism categories.

Revision ID: d1e2f3a4b5c6
Revises: 03d351e145f8
Create Date: 2026-05-17

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: str = '54c59b329caf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Package: generated_brochure_url ---
    op.execute("ALTER TABLE packages ADD COLUMN IF NOT EXISTS generated_brochure_url VARCHAR")

    # --- PackageBoardingPoint: operational reporting fields ---
    op.execute("ALTER TABLE package_boarding_points ADD COLUMN IF NOT EXISTS landmark VARCHAR")
    op.execute("ALTER TABLE package_boarding_points ADD COLUMN IF NOT EXISTS contact_number VARCHAR")
    op.execute("ALTER TABLE package_boarding_points ADD COLUMN IF NOT EXISTS pickup_instructions VARCHAR")
    op.execute("ALTER TABLE package_boarding_points ADD COLUMN IF NOT EXISTS return_drop_info VARCHAR")

    # --- PackageItineraryDay: journey stop fields ---
    op.execute("ALTER TABLE package_itinerary_days ADD COLUMN IF NOT EXISTS timing VARCHAR")
    op.execute("ALTER TABLE package_itinerary_days ADD COLUMN IF NOT EXISTS duration_at_stop VARCHAR")
    op.execute("ALTER TABLE package_itinerary_days ADD COLUMN IF NOT EXISTS image_url VARCHAR")
    op.execute("ALTER TABLE package_itinerary_days ADD COLUMN IF NOT EXISTS meal_included BOOLEAN DEFAULT 'false' NOT NULL")

    # --- PackageGalleryImage: categorization ---
    op.execute("ALTER TABLE package_gallery_images ADD COLUMN IF NOT EXISTS category VARCHAR")

    # --- Expand PolicyType enum with new tourism-specific values ---
    # For PostgreSQL, we need to add values to the existing enum type
    op.execute("ALTER TYPE policytype ADD VALUE IF NOT EXISTS 'SAFETY'")
    op.execute("ALTER TYPE policytype ADD VALUE IF NOT EXISTS 'LUGGAGE'")
    op.execute("ALTER TYPE policytype ADD VALUE IF NOT EXISTS 'FOOD'")
    op.execute("ALTER TYPE policytype ADD VALUE IF NOT EXISTS 'WEATHER'")
    op.execute("ALTER TYPE policytype ADD VALUE IF NOT EXISTS 'BOARDING'")
    op.execute("ALTER TYPE policytype ADD VALUE IF NOT EXISTS 'STAY_RULES'")


def downgrade() -> None:
    op.drop_column('package_gallery_images', 'category')
    op.drop_column('package_itinerary_days', 'meal_included')
    op.drop_column('package_itinerary_days', 'image_url')
    op.drop_column('package_itinerary_days', 'duration_at_stop')
    op.drop_column('package_itinerary_days', 'timing')
    op.drop_column('package_boarding_points', 'return_drop_info')
    op.drop_column('package_boarding_points', 'pickup_instructions')
    op.drop_column('package_boarding_points', 'contact_number')
    op.drop_column('package_boarding_points', 'landmark')
    op.drop_column('packages', 'generated_brochure_url')
    # Note: Removing enum values in PostgreSQL requires recreation — omitted for safety
