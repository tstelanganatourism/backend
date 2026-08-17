"""add package refreshments min

Revision ID: 7a8b9c1d2e41
Revises: 7a8b9c1d2e40
Create Date: 2026-07-21 12:21:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '7a8b9c1d2e41'
down_revision = '7a8b9c1d2e40'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE packages ADD COLUMN IF NOT EXISTS refreshments_min_passengers INTEGER DEFAULT 1 NOT NULL")


def downgrade() -> None:
    op.drop_column('packages', 'refreshments_min_passengers')
