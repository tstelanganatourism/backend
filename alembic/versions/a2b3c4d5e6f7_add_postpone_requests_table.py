"""add_postpone_requests_table

Revision ID: a2b3c4d5e6f7
Revises: 998a93c97ed2
Create Date: 2026-06-15 09:47:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = '998a93c97ed2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'postpone_requests',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('booking_id', sa.BigInteger(), nullable=False),
        sa.Column('reason', sa.String(), nullable=False),
        sa.Column('status', sa.Enum('PENDING', 'APPROVED', 'REJECTED', 'REFUNDED', name='postponestatus'), server_default='PENDING', nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('processed_by', sa.BigInteger(), nullable=True),
        sa.Column('admin_notes', sa.String(), nullable=True),
        sa.Column('requested_new_date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['processed_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_postpone_requests_booking_id'), 'postpone_requests', ['booking_id'], unique=False)
    op.create_index(op.f('ix_postpone_requests_deleted_at'), 'postpone_requests', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_postpone_requests_id'), 'postpone_requests', ['id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_postpone_requests_id'), table_name='postpone_requests')
    op.drop_index(op.f('ix_postpone_requests_deleted_at'), table_name='postpone_requests')
    op.drop_index(op.f('ix_postpone_requests_booking_id'), table_name='postpone_requests')
    op.drop_table('postpone_requests')
    op.execute("DROP TYPE IF EXISTS postponestatus;")
