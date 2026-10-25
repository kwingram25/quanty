"""kind (live / shadow / sim) on orders, fills and settlements

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25 14:00:00.000000

Rows from before this migration are recorded as "sim"; the dashboard reads runs kept in the old
paper/ and live/ folders by their strategies' brokers instead.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("orders", "fills", "settlements")


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("kind", sa.String(), server_default="sim", nullable=False))


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("kind")
