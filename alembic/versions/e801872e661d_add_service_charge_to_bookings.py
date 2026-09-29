"""add_service_charge_to_bookings

Revision ID: e801872e661d
Revises: e801872e661c
Create Date: 2026-09-29 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e801872e661d'
down_revision: Union[str, Sequence[str], None] = 'e801872e661c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS service_charge NUMERIC(12, 2) DEFAULT 0.00 NOT NULL;")


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS service_charge;")
