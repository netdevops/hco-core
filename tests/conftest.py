"""Fixtures for the Postgres tests of every test package.

The tests read `HCO_DATABASE_URL`. An unreachable database fails the test and
never skips it.
"""

import asyncio
import logging
from collections.abc import Iterator

import pytest
import structlog
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.db.migrate import upgrade
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.settings import CoreSettings

REQUESTER = "alice"
DEVICE_ID = "01JDEVICE00000000000000000"
CHANGE_SET_ID = "01JCHANGESET0000000000000A"

TABLES = (
    "device",
    "config_snapshot",
    "change_set",
    "remediation_run",
    "approval_request",
    "approval_decision",
    "audit_event",
    "notification_delivery",
)


@pytest.fixture(scope="session")
def database_url() -> str:
    """Return the URL of a reachable test database, or fail the test."""
    url = CoreSettings().database_url.get_secret_value()
    probe = create_engine(url)
    try:
        with probe.connect():
            pass
    except OperationalError:
        safe_url = make_url(url).render_as_string(hide_password=True)
        pytest.fail(
            f"Postgres at {safe_url} is unreachable. Start it and set "
            "HCO_DATABASE_URL, as docs/developer/setup.md shows.",
            pytrace=False,
        )
    finally:
        probe.dispose()
    return url


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """Give a synchronous engine on the test database.

    Yields:
        The engine, disposed of after the session.

    """
    test_engine = create_engine(database_url)
    yield test_engine
    test_engine.dispose()


@pytest.fixture
def clean_tables(engine: Engine, migrated: None) -> Iterator[None]:
    """Empty every table after the test, on a migrated database."""
    del migrated
    yield
    with engine.begin() as connection:
        connection.execute(
            text(f"TRUNCATE {', '.join(f'hco.{name}' for name in TABLES)} CASCADE")
        )


@pytest.fixture(scope="session")
def migrated(database_url: str) -> None:
    """Upgrade the test database to the head revision once."""
    upgrade(CoreSettings(database_url=SecretStr(database_url)))


@pytest.fixture
def runner() -> Iterator[asyncio.Runner]:
    """Give one event loop for the test.

    Yields:
        The runner, closed after the test.

    """
    with asyncio.Runner() as loop_runner:
        yield loop_runner


@pytest.fixture
def sessions(
    runner: asyncio.Runner, database_url: str, clean_tables: None
) -> Iterator[async_sessionmaker[AsyncSession]]:
    """Give a session factory on the migrated, emptied test database.

    Yields:
        The factory. Its engine is disposed of after the test.

    """
    del clean_tables
    engine = create_database_engine(CoreSettings(database_url=SecretStr(database_url)))
    yield create_sessionmaker(engine)
    runner.run(engine.dispose())


@pytest.fixture
def device_id(engine: Engine, clean_tables: None) -> str:
    """Insert one device."""
    del clean_tables
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO hco.device (id, sot_name, sot_device_id, name, "
                "platform_name, mgmt_address) VALUES (:id, 'yaml', 'r1', 'r1', "
                "'CISCO_IOS', '192.0.2.1')"
            ),
            {"id": DEVICE_ID},
        )
    return DEVICE_ID


@pytest.fixture
def change_set_id(engine: Engine, clean_tables: None) -> str:
    """Insert one change set requested by `REQUESTER`."""
    del clean_tables
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO hco.change_set "
                "(id, title, requested_by, transaction_class, state) "
                "VALUES (:id, 'change', :requester, 'strict', 'DRAFT')"
            ),
            {"id": CHANGE_SET_ID, "requester": REQUESTER},
        )
    return CHANGE_SET_ID


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Put back the root handlers and the structlog defaults after each test."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    root.handlers = handlers
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
