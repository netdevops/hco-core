"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: Sequence[str] | None = ${repr(branch_labels)}
depends_on: Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Upgrade the schema."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade the schema."""
    ${downgrades if downgrades else "pass"}
