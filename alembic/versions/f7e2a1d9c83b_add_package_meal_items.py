"""add package_meal_items table

Revision ID: f7e2a1d9c83b
Revises: cf24ad004179
Create Date: 2026-07-21 11:43:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f7e2a1d9c83b'
down_revision = 'cf24ad004179'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE mealtype AS ENUM ('BREAKFAST', 'LUNCH', 'DINNER', 'SNACKS');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS package_meal_items (
            id          SERIAL PRIMARY KEY,
            package_id  INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
            meal_type   mealtype NOT NULL,
            name        TEXT NOT NULL,
            serving_time TEXT,
            description TEXT,
            cost_per_person NUMERIC(10,2) NOT NULL DEFAULT 0.00,
            is_vegetarian BOOLEAN NOT NULL DEFAULT TRUE,
            day_number  INTEGER,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at  TIMESTAMPTZ
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_package_meal_items_package_id
        ON package_meal_items (package_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS package_meal_items")
    op.execute("DROP TYPE IF EXISTS mealtype")
