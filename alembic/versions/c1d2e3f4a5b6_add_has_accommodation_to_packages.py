"""Add has_accommodation column to packages table.

Revision ID: c1d2e3f4a5b6
Revises: b9c8d7e6f5a4
Create Date: 2026-11-12 11:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b9c8d7e6f5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "packages",
        sa.Column("has_accommodation", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("packages", "has_accommodation")
