"""Add place to packages

Revision ID: add_place_to_packages
Revises: add_duration_to_packages
Create Date: 2026-05-19 19:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_place_to_packages'
down_revision: Union[str, Sequence[str], None] = 'add_duration_to_packages'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('packages', sa.Column('place', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('packages', 'place')
