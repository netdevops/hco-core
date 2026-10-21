"""The Postgres checkpointer that keeps a parked run across processes.

The saver keeps its four tables in schema `hco`, next to the hco-core tables.
`hco_core.db.migrate.upgrade` makes the schema, so run it first.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo

from hco_core.db.models import SCHEMA
from hco_core.settings import PSYCOPG_SCHEME, CoreSettings


def checkpointer_conninfo(settings: CoreSettings) -> str:
    """Return the libpq connection text of the saver, with schema `hco` first.

    The `options` parameter of the database URL is replaced.
    """
    url = settings.database_url.get_secret_value()
    return make_conninfo(
        "postgresql://" + url.removeprefix(PSYCOPG_SCHEME),
        options=f"-c search_path={SCHEMA}",
    )


async def setup_checkpointer(settings: CoreSettings) -> None:
    """Make or migrate the checkpoint tables. Run it after `upgrade`."""
    async with AsyncPostgresSaver.from_conn_string(
        checkpointer_conninfo(settings)
    ) as saver:
        await saver.setup()


@asynccontextmanager
async def open_checkpointer(
    settings: CoreSettings,
) -> AsyncGenerator[AsyncPostgresSaver]:
    """Yield a saver on its own connection, with its tables set up.

    Yields:
        The saver. Its connection closes when the context ends.

    """
    async with AsyncPostgresSaver.from_conn_string(
        checkpointer_conninfo(settings)
    ) as saver:
        await saver.setup()
        yield saver
