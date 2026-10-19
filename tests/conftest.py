"""Fixtures for the tests that need Postgres.

The tests read `HCO_DATABASE_URL`. An unreachable database fails the test and
never skips it.
"""

from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import OperationalError

from hco_core.db.migrate import upgrade
from hco_core.settings import CoreSettings

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
            "HCO_DATABASE_URL, as README.md 'Development' shows.",
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
