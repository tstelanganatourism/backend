"""add_package_room_categories

Revision ID: c1a2b3d4e5f6
Revises: ee61c44354ac
Create Date: 2026-08-05 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c1a2b3d4e5f6'
down_revision = '7a8b9c1d2e42'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── package_categories ────────────────────────────────────────────────────
    op.create_table(
        'package_categories',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sort_order', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('cover_image_url', sa.String(), nullable=True),
        sa.Column('icon', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_package_categories_id'), 'package_categories', ['id'], unique=False)
    op.create_index(op.f('ix_package_categories_slug'), 'package_categories', ['slug'], unique=True)
    op.create_index(op.f('ix_package_categories_is_active'), 'package_categories', ['is_active'], unique=False)
    op.create_index(op.f('ix_package_categories_deleted_at'), 'package_categories', ['deleted_at'], unique=False)

    # ── package_category_assignments ──────────────────────────────────────────
    op.create_table(
        'package_category_assignments',
        sa.Column('category_id', sa.BigInteger(), nullable=False),
        sa.Column('package_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['package_categories.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['package_id'], ['packages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('category_id', 'package_id'),
    )

    # ── room_categories ───────────────────────────────────────────────────────
    op.create_table(
        'room_categories',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sort_order', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('cover_image_url', sa.String(), nullable=True),
        sa.Column('icon', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_room_categories_id'), 'room_categories', ['id'], unique=False)
    op.create_index(op.f('ix_room_categories_slug'), 'room_categories', ['slug'], unique=True)
    op.create_index(op.f('ix_room_categories_is_active'), 'room_categories', ['is_active'], unique=False)
    op.create_index(op.f('ix_room_categories_deleted_at'), 'room_categories', ['deleted_at'], unique=False)

    # ── room_category_assignments ─────────────────────────────────────────────
    op.create_table(
        'room_category_assignments',
        sa.Column('category_id', sa.BigInteger(), nullable=False),
        sa.Column('room_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['room_categories.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['room_id'], ['rooms.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('category_id', 'room_id'),
    )


def downgrade() -> None:
    op.drop_table('room_category_assignments')
    op.drop_index(op.f('ix_room_categories_deleted_at'), table_name='room_categories')
    op.drop_index(op.f('ix_room_categories_is_active'), table_name='room_categories')
    op.drop_index(op.f('ix_room_categories_slug'), table_name='room_categories')
    op.drop_index(op.f('ix_room_categories_id'), table_name='room_categories')
    op.drop_table('room_categories')
    op.drop_table('package_category_assignments')
    op.drop_index(op.f('ix_package_categories_deleted_at'), table_name='package_categories')
    op.drop_index(op.f('ix_package_categories_is_active'), table_name='package_categories')
    op.drop_index(op.f('ix_package_categories_slug'), table_name='package_categories')
    op.drop_index(op.f('ix_package_categories_id'), table_name='package_categories')
    op.drop_table('package_categories')
