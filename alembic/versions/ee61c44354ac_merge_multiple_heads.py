"""merge multiple heads

Revision ID: ee61c44354ac
Revises: 61570d51b011, a1b2c3d4e5f7
Create Date: 2026-06-20 14:54:02.956726

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ee61c44354ac'
down_revision: Union[str, Sequence[str], None] = ('61570d51b011', 'a1b2c3d4e5f7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
