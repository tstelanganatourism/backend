"""rename razorpay columns to gateway agnostic names

Revision ID: a9f3b2c1d8e7
Revises: f9d65f0ca8a6
Create Date: 2026-06-11

Renames:
  payments.razorpay_order_id   → pg_order_id
  payments.razorpay_payment_id → pg_payment_id
  payments.razorpay_signature  → pg_signature
  payments.payment_method default RAZORPAY → PHONEPE
  payments.collected_by_type default RAZORPAY → PHONEPE
  booking_drafts.razorpay_order_id → pg_transaction_id
  booking_drafts adds payment_gateway column
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9f3b2c1d8e7'
down_revision = 'f9d65f0ca8a6'
branch_labels = None
depends_on = '627f59f177ae'


def upgrade() -> None:
    # ── payments table ──────────────────────────────────────────────────────
    op.alter_column('payments', 'razorpay_order_id',
                    new_column_name='pg_order_id',
                    existing_type=sa.String(),
                    existing_nullable=True)

    op.alter_column('payments', 'razorpay_payment_id',
                    new_column_name='pg_payment_id',
                    existing_type=sa.String(),
                    existing_nullable=True)

    op.alter_column('payments', 'razorpay_signature',
                    new_column_name='pg_signature',
                    existing_type=sa.String(length=255),
                    existing_nullable=True)

    # Update default value for payment_method (RAZORPAY → PHONEPE)
    op.alter_column('payments', 'payment_method',
                    existing_type=sa.String(length=50),
                    server_default='PHONEPE',
                    existing_nullable=False)

    # Update default value for collected_by_type (RAZORPAY → PHONEPE)
    op.alter_column('payments', 'collected_by_type',
                    existing_type=sa.String(length=50),
                    server_default='PHONEPE',
                    existing_nullable=False)

    # Also update existing RAZORPAY rows to PHONEPE (retroactively — they were all PhonePe)
    op.execute(
        "UPDATE payments SET payment_method = 'PHONEPE' WHERE payment_method = 'RAZORPAY'"
    )
    op.execute(
        "UPDATE payments SET collected_by_type = 'PHONEPE' WHERE collected_by_type = 'RAZORPAY'"
    )

    # ── booking_drafts table ────────────────────────────────────────────────
    op.alter_column('booking_drafts', 'razorpay_order_id',
                    new_column_name='pg_transaction_id',
                    existing_type=sa.String(),
                    existing_nullable=True)

    # Add new payment_gateway column to drafts
    op.add_column('booking_drafts',
                  sa.Column('payment_gateway', sa.String(length=20), server_default='PHONEPE', nullable=True))


def downgrade() -> None:
    # ── booking_drafts table ────────────────────────────────────────────────
    op.drop_column('booking_drafts', 'payment_gateway')

    op.alter_column('booking_drafts', 'pg_transaction_id',
                    new_column_name='razorpay_order_id',
                    existing_type=sa.String(),
                    existing_nullable=True)

    # ── payments table ──────────────────────────────────────────────────────
    op.alter_column('payments', 'pg_order_id',
                    new_column_name='razorpay_order_id',
                    existing_type=sa.String(),
                    existing_nullable=True)

    op.alter_column('payments', 'pg_payment_id',
                    new_column_name='razorpay_payment_id',
                    existing_type=sa.String(),
                    existing_nullable=True)

    op.alter_column('payments', 'pg_signature',
                    new_column_name='razorpay_signature',
                    existing_type=sa.String(length=255),
                    existing_nullable=True)

    op.alter_column('payments', 'payment_method',
                    existing_type=sa.String(length=50),
                    server_default='RAZORPAY',
                    existing_nullable=False)

    op.alter_column('payments', 'collected_by_type',
                    existing_type=sa.String(length=50),
                    server_default='RAZORPAY',
                    existing_nullable=False)
