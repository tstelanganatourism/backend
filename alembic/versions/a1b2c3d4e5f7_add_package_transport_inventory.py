"""add_package_transport_inventory

Revision ID: a1b2c3d4e5f7
Revises: 998a93c97ed2
Create Date: 2026-06-20 09:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f7'
down_revision: Union[str, Sequence[str], None] = '998a93c97ed2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create package_transport_inventory table."""
    op.create_table(
        'package_transport_inventory',
        sa.Column('transport_option_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('available_count', sa.Integer(), server_default='1', nullable=False),
        sa.Column('booked_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('is_closed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('price_override', sa.Numeric(10, 2), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['transport_option_id'],
            ['package_transport_options.id'],
            name=op.f('fk_package_transport_inventory_transport_option_id_package_transport_options'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_package_transport_inventory')),
        sa.UniqueConstraint('transport_option_id', 'date', name='uq_transport_inventory'),
    )
    op.create_index(
        op.f('ix_package_transport_inventory_transport_option_id'),
        'package_transport_inventory',
        ['transport_option_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_package_transport_inventory_date'),
        'package_transport_inventory',
        ['date'],
        unique=False,
    )
    op.create_index(
        op.f('ix_package_transport_inventory_id'),
        'package_transport_inventory',
        ['id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_package_transport_inventory_deleted_at'),
        'package_transport_inventory',
        ['deleted_at'],
        unique=False,
    )


def downgrade() -> None:
    """Drop package_transport_inventory table."""
    op.drop_index(op.f('ix_package_transport_inventory_deleted_at'), table_name='package_transport_inventory')
    op.drop_index(op.f('ix_package_transport_inventory_id'), table_name='package_transport_inventory')
    op.drop_index(op.f('ix_package_transport_inventory_date'), table_name='package_transport_inventory')
    op.drop_index(op.f('ix_package_transport_inventory_transport_option_id'), table_name='package_transport_inventory')
    op.drop_table('package_transport_inventory')
