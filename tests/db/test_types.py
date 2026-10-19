"""`PlatformName` stores a platform by name, never by value (hard rule 5)."""

import asyncio
from collections.abc import Sequence

import pytest
from hier_config.models import Platform
from pydantic import SecretStr
from sqlalchemy import Engine, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from hco_core.db.models import Device
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.db.types import ULID_LENGTH, PlatformName, new_id
from hco_core.engine.errors import UnknownPlatformError
from hco_core.settings import CoreSettings

DIALECT = postgresql.dialect()

pytestmark = pytest.mark.usefixtures("clean_tables")


@pytest.mark.parametrize("platform", list(Platform))
def test_every_platform_member_binds_as_its_name(platform: Platform) -> None:
    """A member binds as `.name`, never as its positional `.value`."""
    column_type = PlatformName()

    assert column_type.process_bind_param(platform, DIALECT) == platform.name
    assert column_type.process_bind_param(platform.name, DIALECT) == platform.name


def test_none_binds_and_loads_as_none() -> None:
    """A NULL stays NULL in both directions."""
    column_type = PlatformName()

    assert column_type.process_bind_param(None, DIALECT) is None
    assert column_type.process_result_value(None, DIALECT) is None


@pytest.mark.parametrize("name", ["NOPE_OS", "3"])
def test_an_unknown_platform_or_a_value_is_refused(name: str) -> None:
    """A name with no driver, or a positional value, never reaches a row."""
    with pytest.raises(UnknownPlatformError):
        PlatformName().process_bind_param(name, DIALECT)


def test_new_id_is_a_sortable_ulid() -> None:
    """Keys are 26 characters and sort by creation time."""
    first, second = new_id(), new_id()

    assert len(first) == ULID_LENGTH
    assert first[:10] <= second[:10]


async def store_and_load(database_url: str, spellings: Sequence[str]) -> list[str]:
    """Store one device for each spelling, and return the stored names in order."""
    engine = create_database_engine(CoreSettings(database_url=SecretStr(database_url)))
    sessions = create_sessionmaker(engine)
    try:
        async with sessions.begin() as session:
            for index, spelling in enumerate(spellings):
                session.add(
                    Device(
                        sot_name="yaml",
                        sot_device_id=f"r{index:02d}",
                        name=f"r{index:02d}",
                        platform_name=spelling,
                        mgmt_address="192.0.2.1",
                    )
                )
        async with sessions() as session:
            devices = await session.scalars(select(Device).order_by(Device.name))
            return [device.platform_name for device in devices]
    finally:
        await engine.dispose()


def test_three_spellings_of_one_platform_store_one_name(database_url: str) -> None:
    """`Platform.CISCO_IOS`, `cisco_ios`, and `CISCO_IOS` store `CISCO_IOS`."""
    stored = asyncio.run(
        store_and_load(database_url, [Platform.CISCO_IOS, "cisco_ios", "CISCO_IOS"])
    )

    assert stored == ["CISCO_IOS", "CISCO_IOS", "CISCO_IOS"]


def test_every_platform_member_is_stored_as_its_name(database_url: str) -> None:
    """Each member reaches the row as `.name`, in the order it was stored."""
    platforms = list(Platform)

    stored = asyncio.run(store_and_load(database_url, platforms))

    assert stored == [platform.name for platform in platforms]


@pytest.mark.parametrize("value", ["3", "cisco_ios"])
def test_the_column_refuses_a_value_or_a_lower_case_name(
    engine: Engine, value: str
) -> None:
    """A direct INSERT past `PlatformName` cannot store a value or a lower-case name."""
    with (
        pytest.raises(IntegrityError, match="ck_device_platform_name"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO hco.device (id, sot_name, sot_device_id, name, "
                "platform_name, mgmt_address) VALUES "
                "('01JDEVICE0000000000000000A', 'yaml', 'r1', 'r1', :value, "
                "'192.0.2.1')"
            ),
            {"value": value},
        )
