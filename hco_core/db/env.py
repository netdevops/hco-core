"""Run the hco-core migrations on the connection that `hco_core.db.migrate` gives.

The version table `alembic_version` is in schema `hco`. The HCO AI layer keeps
its own chain in `alembic_version_ai`.
"""

from alembic import context
from sqlalchemy import Connection, text

from hco_core.db.models import SCHEMA, Base


def given_connection() -> Connection:
    """Return the connection that `hco_core.db.migrate` puts in the config.

    Raises:
        RuntimeError: If the `alembic` command runs this file without it.

    """
    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        message = (
            "run the migrations with hco_core.db.migrate.upgrade() or "
            "downgrade(), not with the alembic command"
        )
        raise RuntimeError(message)  # ruff: ignore[type-check-without-type-error]
    return connection


def run_migrations(connection: Connection) -> None:
    """Make schema `hco`, then run the migrations in it."""
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations(given_connection())
