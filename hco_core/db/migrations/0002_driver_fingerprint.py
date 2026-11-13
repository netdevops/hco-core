"""Add the driver fingerprint of a run, and the index that finds the runs of a device.

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
DEVICE_RUNS_INDEX = "ix_remediation_run_device_id_id"


def upgrade() -> None:
    """Add the nullable column and the index."""
    op.add_column(
        "remediation_run",
        sa.Column("driver_fingerprint", sa.String(64)),
        schema=SCHEMA,
    )
    op.create_index(
        DEVICE_RUNS_INDEX, "remediation_run", ["device_id", "id"], schema=SCHEMA
    )


def downgrade() -> None:
    """Drop the index and the column."""
    op.drop_index(DEVICE_RUNS_INDEX, table_name="remediation_run", schema=SCHEMA)
    op.drop_column("remediation_run", "driver_fingerprint", schema=SCHEMA)
