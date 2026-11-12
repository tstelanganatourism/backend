"""Add occupancy and combination_pricing to package_variants table.

Revision ID: b9c8d7e6f5a4
Revises: f1a2b3c4d5e6
Create Date: 2026-11-12 11:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op


revision: str = "b9c8d7e6f5a4"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add occupancy column: default 1 (per-person), nullable=False
    op.add_column(
        "package_variants",
        sa.Column("occupancy", sa.Integer(), nullable=False, server_default="1"),
    )
    # Add combination_pricing column: JSONB array of {adults, children, price, weekend_price}
    op.add_column(
        "package_variants",
        sa.Column("combination_pricing", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("package_variants", "combination_pricing")
    op.drop_column("package_variants", "occupancy")
