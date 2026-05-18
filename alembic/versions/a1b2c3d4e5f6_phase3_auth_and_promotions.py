"""Phase-3: Add promotions table and promotion enum types.

Revision ID: a1b2c3d4e5f6
Revises: 03d351e145f8
Create Date: 2026-05-16 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "03d351e145f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop existing enums just in case of a previous failed run
    op.execute("DROP TYPE IF EXISTS promotiontype CASCADE")
    op.execute("DROP TYPE IF EXISTS promotiontarget CASCADE")
    op.execute("DROP TYPE IF EXISTS promotionbadge CASCADE")

    # ── Promotions table ───────────────────────────────────────────────────────
    op.create_table(
        "promotions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), onupdate=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sort_order", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("subtitle", sa.String(length=512), nullable=True),
        sa.Column("icon_emoji", sa.String(length=8), nullable=True),
        sa.Column("badge", sa.Enum("NONE", "LIMITED_TIME", "BESTSELLER", "NEW_OFFER", "FESTIVAL_OFFER", "SUMMER_SPECIAL", name="promotionbadge", create_type=False), nullable=False, server_default="NONE"),
        sa.Column("type", sa.Enum("FLAT_DISCOUNT", "PERCENT_DISCOUNT", "INFORMATIONAL", "FREE_SERVICE", "CAMPAIGN", name="promotiontype", create_type=False), nullable=False),
        sa.Column("target", sa.Enum("ALL", "TOURS_ONLY", "TRIPS_ONLY", "ROOMS_ONLY", "AP_REGION", "TS_REGION", "SPECIFIC_PACKAGES", name="promotiontarget", create_type=False), nullable=False, server_default="ALL"),
        sa.Column("discount_value", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("cta_label", sa.String(length=64), nullable=True),
        sa.Column("cta_url", sa.String(length=512), nullable=True),
        sa.Column("bg_gradient", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_promotions"),
    )

    # Indexes for performant public banner query
    op.create_index("ix_promotions_is_active", "promotions", ["is_active"])
    op.create_index("ix_promotions_valid_from", "promotions", ["valid_from"])
    op.create_index("ix_promotions_valid_until", "promotions", ["valid_until"])
    op.create_index("ix_promotions_sort_order", "promotions", ["sort_order"])
    op.create_index("ix_promotions_deleted_at", "promotions", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_promotions_deleted_at", table_name="promotions")
    op.drop_index("ix_promotions_sort_order", table_name="promotions")
    op.drop_index("ix_promotions_valid_until", table_name="promotions")
    op.drop_index("ix_promotions_valid_from", table_name="promotions")
    op.drop_index("ix_promotions_is_active", table_name="promotions")
    op.drop_table("promotions")

    sa.Enum(name="promotionbadge").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="promotiontarget").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="promotiontype").drop(op.get_bind(), checkfirst=True)
