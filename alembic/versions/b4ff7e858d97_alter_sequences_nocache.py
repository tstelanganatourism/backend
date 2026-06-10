"""alter_sequences_nocache

Revision ID: b4ff7e858d97
Revises: 1904cdf2f96d
Create Date: 2026-06-08 23:25:59.450168

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4ff7e858d97'
down_revision: Union[str, Sequence[str], None] = '1904cdf2f96d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER SEQUENCE IF EXISTS booking_seq_bt CACHE 1")
    op.execute("ALTER SEQUENCE IF EXISTS booking_seq_ss CACHE 1")
    op.execute("ALTER SEQUENCE IF EXISTS booking_seq_ac CACHE 1")


def downgrade() -> None:
    """Downgrade schema."""
    pass
