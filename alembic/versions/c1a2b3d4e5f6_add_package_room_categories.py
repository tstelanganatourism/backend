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
    op.execute("""
    CREATE TABLE IF NOT EXISTS package_categories (
        id BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        deleted_at TIMESTAMP WITH TIME ZONE,
        sort_order BIGINT DEFAULT 0 NOT NULL,
        name VARCHAR NOT NULL,
        slug VARCHAR NOT NULL,
        description VARCHAR,
        cover_image_url VARCHAR,
        icon VARCHAR,
        is_active BOOLEAN DEFAULT true NOT NULL
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_package_categories_id ON package_categories (id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_package_categories_slug ON package_categories (slug)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_package_categories_is_active ON package_categories (is_active)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_package_categories_deleted_at ON package_categories (deleted_at)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS package_category_assignments (
        category_id BIGINT NOT NULL REFERENCES package_categories(id) ON DELETE CASCADE,
        package_id BIGINT NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
        PRIMARY KEY (category_id, package_id)
    )
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS room_categories (
        id BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        deleted_at TIMESTAMP WITH TIME ZONE,
        sort_order BIGINT DEFAULT 0 NOT NULL,
        name VARCHAR NOT NULL,
        slug VARCHAR NOT NULL,
        description VARCHAR,
        cover_image_url VARCHAR,
        icon VARCHAR,
        is_active BOOLEAN DEFAULT true NOT NULL
    )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_room_categories_id ON room_categories (id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_room_categories_slug ON room_categories (slug)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_room_categories_is_active ON room_categories (is_active)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_room_categories_deleted_at ON room_categories (deleted_at)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS room_category_assignments (
        category_id BIGINT NOT NULL REFERENCES room_categories(id) ON DELETE CASCADE,
        room_id BIGINT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
        PRIMARY KEY (category_id, room_id)
    )
    """)


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
