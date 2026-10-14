"""Allow administrator-created APTDC manual ticket records.

Revision ID: e5f6a7b8c9d0
Revises: e801872e661d
Create Date: 2026-10-14 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "e801872e661d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Manual APTDC receipts are booking records without a package or room
    # inventory target.
    op.execute('ALTER TABLE bookings DROP CONSTRAINT IF EXISTS "chk_booking_target"')
    op.execute('ALTER TABLE bookings DROP CONSTRAINT IF EXISTS "ck_bookings_chk_booking_target"')
    op.create_check_constraint(
        "chk_booking_target",
        "bookings",
        "room_variant_id IS NULL OR variant_id IS NULL",
    )
    op.execute("CREATE SEQUENCE IF NOT EXISTS booking_seq_aptdc START 1000 CACHE 1")


def downgrade() -> None:
    op.execute('ALTER TABLE bookings DROP CONSTRAINT IF EXISTS "chk_booking_target"')
    op.execute('ALTER TABLE bookings DROP CONSTRAINT IF EXISTS "ck_bookings_chk_booking_target"')
    op.create_check_constraint(
        "chk_booking_target",
        "bookings",
        "(room_variant_id IS NOT NULL AND variant_id IS NULL) OR (room_variant_id IS NULL AND variant_id IS NOT NULL)",
    )
    op.execute("DROP SEQUENCE IF EXISTS booking_seq_aptdc")
