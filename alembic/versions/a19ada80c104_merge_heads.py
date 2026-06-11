"""merge heads

Revision ID: a19ada80c104
Revises: b4ff7e858d97, a9f3b2c1d8e7, d8873fd7d851
Create Date: 2026-06-11 19:25:11.126529

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a19ada80c104'
down_revision: Union[str, Sequence[str], None] = ('b4ff7e858d97', 'a9f3b2c1d8e7', 'd8873fd7d851')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
