"""add_passenger_relationship_and_primary_fields

Revision ID: e1a2b3c4d5f6
Revises: 0460d2bd65c7
Create Date: 2026-05-20 13:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e1a2b3c4d5f6'
down_revision: Union[str, Sequence[str], None] = 'add_place_to_packages'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add relationship_to_lead and is_primary columns to booking_passengers."""
    op.add_column('booking_passengers', sa.Column('relationship_to_lead', sa.String(length=50), nullable=True))
    op.add_column('booking_passengers', sa.Column('is_primary', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    """Remove relationship_to_lead and is_primary columns."""
    op.drop_column('booking_passengers', 'is_primary')
    op.drop_column('booking_passengers', 'relationship_to_lead')
