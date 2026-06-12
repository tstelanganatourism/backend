"""Add student package support

Revision ID: e1f2a3b4c5d6
Revises: a19ada80c104
Create Date: 2026-06-12 02:47:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'a19ada80c104'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add all columns required for student package support."""

    # ── packages table ────────────────────────────────────────────────────────
    op.add_column('packages', sa.Column(
        'is_student_package', sa.Boolean(),
        nullable=False, server_default=sa.text('false')
    ))
    op.add_column('packages', sa.Column(
        'refreshment_student_price', sa.Numeric(10, 2), nullable=True
    ))

    # ── package_variants table ────────────────────────────────────────────────
    op.add_column('package_variants', sa.Column(
        'student_price', sa.Numeric(10, 2), nullable=True
    ))
    op.add_column('package_variants', sa.Column(
        'weekend_student_price', sa.Numeric(10, 2), nullable=True
    ))

    # ── package_transport_options table ───────────────────────────────────────
    op.add_column('package_transport_options', sa.Column(
        'student_price', sa.Numeric(10, 2), nullable=True
    ))
    op.add_column('package_transport_options', sa.Column(
        'weekend_student_price', sa.Numeric(10, 2), nullable=True
    ))

    # ── bookings table ────────────────────────────────────────────────────────
    op.add_column('bookings', sa.Column(
        'student_count', sa.Integer(),
        nullable=False, server_default=sa.text('0')
    ))

    # ── booking_passengers table ──────────────────────────────────────────────
    op.add_column('booking_passengers', sa.Column(
        'student_class', sa.String(100), nullable=True
    ))


def downgrade() -> None:
    """Remove all student package columns."""
    op.drop_column('booking_passengers', 'student_class')
    op.drop_column('bookings', 'student_count')
    op.drop_column('package_transport_options', 'weekend_student_price')
    op.drop_column('package_transport_options', 'student_price')
    op.drop_column('package_variants', 'weekend_student_price')
    op.drop_column('package_variants', 'student_price')
    op.drop_column('packages', 'refreshment_student_price')
    op.drop_column('packages', 'is_student_package')
