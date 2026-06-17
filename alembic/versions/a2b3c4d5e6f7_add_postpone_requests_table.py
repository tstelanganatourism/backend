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
    # Table already exists in DB, so this migration is just for code compatibility.
    pass


def downgrade() -> None:
    op.drop_table('postpone_requests')
