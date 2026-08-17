"""add_is_weekend_only_to_coupons

Revision ID: 3b87ef55f3f6
Revises: ee61c44354ac
Create Date: 2026-06-22 19:04:36.060033

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b87ef55f3f6'
down_revision: Union[str, Sequence[str], None] = 'ee61c44354ac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE coupons ADD COLUMN IF NOT EXISTS is_weekend_only BOOLEAN DEFAULT 'false' NOT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('coupons', 'is_weekend_only')
