"""add package food fields

Revision ID: 7a8b9c1d2e3f
Revises: f7e2a1d9c83b
Create Date: 2026-07-21 12:05:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '7a8b9c1d2e3f'
down_revision = 'f7e2a1d9c83b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE packages ADD COLUMN IF NOT EXISTS has_food_option BOOLEAN DEFAULT 'false' NOT NULL")
    op.execute("ALTER TABLE packages ADD COLUMN IF NOT EXISTS food_adult_price NUMERIC(10, 2)")
    op.execute("ALTER TABLE packages ADD COLUMN IF NOT EXISTS food_child_price NUMERIC(10, 2)")
    op.execute("ALTER TABLE packages ADD COLUMN IF NOT EXISTS food_student_price NUMERIC(10, 2)")


def downgrade() -> None:
    op.drop_column('packages', 'food_student_price')
    op.drop_column('packages', 'food_child_price')
    op.drop_column('packages', 'food_adult_price')
    op.drop_column('packages', 'has_food_option')
