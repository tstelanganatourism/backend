"""merge heads

Revision ID: 0a55a16d52e9
Revises: e1f2a3b4c5d6, f8c15b05697f
Create Date: 2026-06-13 18:42:17.051769

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0a55a16d52e9'
down_revision: Union[str, Sequence[str], None] = ('e1f2a3b4c5d6', 'f8c15b05697f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
