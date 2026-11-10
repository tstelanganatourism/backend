"""Add admin-controlled public_title column to rooms table.

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-11-10 17:35:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("public_title", sa.String(), nullable=True))
    op.create_index("ix_rooms_public_title", "rooms", ["public_title"], unique=False)

    # Backfill existing accommodations cleanly so admins have explicit editable public titles
    # without ever exposing private lodge/hotel vendor names.
    op.execute(
        """
        UPDATE rooms
        SET public_title = CASE
            WHEN slug = 'vashista-residency-bhadrachalam' THEN 'Deluxe & Family Suite A/C'
            WHEN slug = 'family-mini-suite-ac' THEN 'Family Mini Suite A/C'
            WHEN slug LIKE 'godavari-haritha-resort%' THEN 'Standard A/C Room'
            WHEN slug LIKE 'hotel-anand-regency%' THEN '2-Bed & 4-Bed A/C Deluxe Room'
            WHEN slug LIKE 'kolluru-bamboo-river-huts%' THEN 'Kolluru Bamboo River Huts'
            ELSE lodge_name
        END
        WHERE public_title IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_rooms_public_title", table_name="rooms")
    op.drop_column("rooms", "public_title")
