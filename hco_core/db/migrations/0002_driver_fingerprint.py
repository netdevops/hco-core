"""Add the driver fingerprint to the remediation run.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "hco"


def upgrade() -> None:
    """Add the nullable column."""
    op.add_column(
        "remediation_run",
        sa.Column("driver_fingerprint", sa.String(64)),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("remediation_run", "driver_fingerprint", schema=SCHEMA)
