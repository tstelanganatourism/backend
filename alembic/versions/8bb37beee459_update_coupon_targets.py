"""update_coupon_targets

Revision ID: 8bb37beee459
Revises: da5425141eff
Create Date: 2026-05-21 20:20:24.053442

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8bb37beee459'
down_revision: Union[str, Sequence[str], None] = 'da5425141eff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


from sqlalchemy.dialects import postgresql

def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('coupons', sa.Column('applicable_package_ids', postgresql.ARRAY(sa.Integer()), server_default='{}', nullable=False))
    op.add_column('coupons', sa.Column('applicable_room_ids', postgresql.ARRAY(sa.Integer()), server_default='{}', nullable=False))
    op.drop_column('coupons', 'package_id')

def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE coupons ADD COLUMN IF NOT EXISTS package_id TEXT")
    op.create_foreign_key('coupons_package_id_fkey', 'coupons', 'packages', ['package_id'], ['id'], ondelete='SET NULL')
    op.drop_column('coupons', 'applicable_room_ids')
    op.drop_column('coupons', 'applicable_package_ids')
