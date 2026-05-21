"""Add duration to packages

Revision ID: add_duration_to_packages
Revises: d8873fd7d851
Create Date: 2026-05-19 19:02:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_duration_to_packages'
down_revision: Union[str, Sequence[str], None] = 'd8873fd7d851'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('packages', sa.Column('duration', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('packages', 'duration')
