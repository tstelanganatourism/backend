"""add_booking_sequences

Revision ID: 24da6073143d
Revises: 91455a7d168b
Create Date: 2026-05-27 10:52:41.261150

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '24da6073143d'
down_revision: Union[str, Sequence[str], None] = '91455a7d168b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SEQUENCE IF NOT EXISTS booking_seq_bt START 1000")
    op.execute("CREATE SEQUENCE IF NOT EXISTS booking_seq_ss START 1000")
    op.execute("CREATE SEQUENCE IF NOT EXISTS booking_seq_ac START 1000")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP SEQUENCE IF EXISTS booking_seq_bt")
    op.execute("DROP SEQUENCE IF EXISTS booking_seq_ss")
    op.execute("DROP SEQUENCE IF EXISTS booking_seq_ac")
