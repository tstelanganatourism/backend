"""add package food fields

Revision ID: 7a8b9c1d2e3f
Revises: f7e2a1d9c83b
Create Date: 2026-07-21 12:05:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '7a8b9c1d2e3f'
down_revision = 'f7e2a1d9c83b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('packages', sa.Column('has_food_option', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('packages', sa.Column('food_adult_price', sa.Numeric(10, 2), nullable=True))
    op.add_column('packages', sa.Column('food_child_price', sa.Numeric(10, 2), nullable=True))
    op.add_column('packages', sa.Column('food_student_price', sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('packages', 'food_student_price')
    op.drop_column('packages', 'food_child_price')
    op.drop_column('packages', 'food_adult_price')
    op.drop_column('packages', 'has_food_option')
