"""Fixtures for the service tests: an event loop, sessions, and seed rows."""

import asyncio
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.settings import CoreSettings

REQUESTER = "alice"
DEVICE_ID = "01JDEVICE00000000000000000"
CHANGE_SET_ID = "01JCHANGESET0000000000000A"


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
