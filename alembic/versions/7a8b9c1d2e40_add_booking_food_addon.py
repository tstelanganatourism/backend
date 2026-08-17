"""add booking food addon

Revision ID: 7a8b9c1d2e40
Revises: 7a8b9c1d2e3f
Create Date: 2026-07-21 12:10:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '7a8b9c1d2e40'
down_revision = '7a8b9c1d2e3f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS has_food_addon BOOLEAN DEFAULT 'false' NOT NULL")


def downgrade() -> None:
    op.drop_column('bookings', 'has_food_addon')
