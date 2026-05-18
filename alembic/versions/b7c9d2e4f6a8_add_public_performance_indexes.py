"""add_public_performance_indexes

Revision ID: b7c9d2e4f6a8
Revises: 6069a223584e
Create Date: 2026-05-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "b7c9d2e4f6a8"
down_revision: Union[str, Sequence[str], None] = "6069a223584e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_packages_public_priority "
        "ON packages (is_active, deleted_at, order_priority, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_packages_public_featured "
        "ON packages (is_featured, is_active, deleted_at, order_priority, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_packages_admin_listing "
        "ON packages (deleted_at, status, order_priority, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_package_variants_public_price "
        "ON package_variants (package_id, is_active, deleted_at, adult_price)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rooms_public_priority "
        "ON rooms (is_active, deleted_at, order_priority, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rooms_public_featured "
        "ON rooms (is_featured, is_active, deleted_at, order_priority, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rooms_admin_listing "
        "ON rooms (deleted_at, status, order_priority, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_room_variants_public_price "
        "ON room_variants (room_id, is_active, deleted_at, weekday_price)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_package_inventory_variant_date "
        "ON package_variant_inventory (variant_id, date)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_room_slot_inventory_room_date "
        "ON room_slot_inventory (room_id, date)"
    )


def downgrade() -> None:
    for index_name in (
        "ix_room_slot_inventory_room_date",
        "ix_package_inventory_variant_date",
        "ix_room_variants_public_price",
        "ix_rooms_admin_listing",
        "ix_rooms_public_featured",
        "ix_rooms_public_priority",
        "ix_package_variants_public_price",
        "ix_packages_admin_listing",
        "ix_packages_public_featured",
        "ix_packages_public_priority",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
