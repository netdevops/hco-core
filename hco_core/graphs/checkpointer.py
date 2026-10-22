"""The Postgres checkpointer that keeps a parked run across processes.

The saver keeps its four tables in schema `hco`, next to the hco-core tables.
`setup_checkpointer` makes them, one time, as a deploy step after
`hco_core.db.migrate.upgrade`. After that, the runtime role needs only `USAGE`
on the schema and DML grants on the tables.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from hco_core.db.models import SCHEMA
from hco_core.settings import PSYCOPG_SCHEME, CoreSettings


class InvalidDatabaseUrlError(ValueError):
    """The database URL does not parse. The message never shows the URL."""

    def __init__(self) -> None:
        super().__init__("HCO_DATABASE_URL is not a valid Postgres URL")


def checkpointer_conninfo(settings: CoreSettings) -> str:
    """Return the libpq connection text of the saver, with schema `hco` first.

    The search path is added after the `options` of the URL.

    Raises:
        InvalidDatabaseUrlError: If the URL does not parse.

    """
    url = "postgresql://" + settings.database_url.get_secret_value().removeprefix(
        PSYCOPG_SCHEME
    )
    params = _parse(url)
    if params is None:
        raise InvalidDatabaseUrlError
    given = params.get("options")
    search_path = f"-c search_path={SCHEMA}"
    return make_conninfo(
        url, options=f"{given} {search_path}" if given else search_path
    )


def _parse(url: str) -> dict[str, str | int | None] | None:
    try:
        return conninfo_to_dict(url)
    except ProgrammingError:
        return None


async def setup_checkpointer(settings: CoreSettings) -> None:
    """Make or migrate the checkpoint tables, one time, after `upgrade`."""
    async with AsyncPostgresSaver.from_conn_string(
        checkpointer_conninfo(settings)
    ) as saver:
        await saver.setup()


@asynccontextmanager
async def open_checkpointer(
    settings: CoreSettings,
) -> AsyncGenerator[AsyncPostgresSaver]:
    """Yield a saver on its own connection. It makes no table.

    Yields:
        The saver. Its connection closes when the context ends.

    """
    async with AsyncPostgresSaver.from_conn_string(
        checkpointer_conninfo(settings)
    ) as saver:
        yield saver
