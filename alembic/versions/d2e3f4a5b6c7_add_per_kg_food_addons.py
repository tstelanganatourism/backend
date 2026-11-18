"""Add kilogram based package food pricing.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa


revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("packages", sa.Column("food_price_per_kg", sa.Numeric(10, 2), nullable=True))
    op.add_column(
        "package_extras",
        sa.Column("pricing_unit", sa.String(length=20), server_default="PER_PERSON", nullable=False),
    )
    op.add_column("package_extras", sa.Column("price_per_kg", sa.Numeric(10, 2), nullable=True))

    # Keep existing food add-ons available while changing their pricing basis.
    op.execute(
        """
        UPDATE package_extras
        SET pricing_unit = 'PER_KG',
            price_per_kg = COALESCE(adult_price, child_price, student_price, 0)
        WHERE lower(title) LIKE '%food%'
           OR lower(title) LIKE '%meal%'
           OR lower(title) LIKE '%catering%'
        """
    )
    op.execute(
        """
        UPDATE packages
        SET food_price_per_kg = COALESCE(food_adult_price, food_student_price, food_child_price, 0)
        WHERE has_food_option = TRUE AND food_price_per_kg IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("package_extras", "price_per_kg")
    op.drop_column("package_extras", "pricing_unit")
    op.drop_column("packages", "food_price_per_kg")
