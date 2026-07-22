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
    op.add_column('bookings', sa.Column('has_food_addon', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('bookings', 'has_food_addon')
