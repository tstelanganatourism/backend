"""add_inventory_controls_is_closed_price_override

Revision ID: c9a1e2d3f4b5
Revises: 84e3818feb23
Create Date: 2026-05-17 11:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'c9a1e2d3f4b5'
down_revision: Union[str, Sequence[str], None] = '84e3818feb23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add is_closed column to package_variant_inventory
    op.add_column(
        'package_variant_inventory',
        sa.Column('is_closed', sa.Boolean(), nullable=False, server_default='false')
    )
    # Add price_override column (nullable — NULL means use variant base price)
    op.add_column(
        'package_variant_inventory',
        sa.Column('price_override', sa.Numeric(10, 2), nullable=True)
    )
    # Update total_capacity default to 500 for future rows
    op.alter_column(
        'package_variant_inventory',
        'total_capacity',
        existing_type=sa.Integer(),
        server_default='500',
        existing_nullable=False
    )


def downgrade() -> None:
    op.drop_column('package_variant_inventory', 'price_override')
    op.drop_column('package_variant_inventory', 'is_closed')
    op.alter_column(
        'package_variant_inventory',
        'total_capacity',
        existing_type=sa.Integer(),
        server_default=None,
        existing_nullable=False
    )
