"""add room slot prices

Revision ID: 7a8b9c1d2e42
Revises: 7a8b9c1d2e41
Create Date: 2026-07-21 12:38:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '7a8b9c1d2e42'
down_revision = '7a8b9c1d2e41'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('room_slot_inventory', sa.Column('weekday_price', sa.Numeric(10, 2), nullable=True))
    op.add_column('room_slot_inventory', sa.Column('weekend_price', sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('room_slot_inventory', 'weekend_price')
    op.drop_column('room_slot_inventory', 'weekday_price')
