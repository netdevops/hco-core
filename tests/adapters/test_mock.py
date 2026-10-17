"""The mock connection replays transcripts and never writes to a device."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.errors import (
    AdapterConfigError,
    CommandNotAllowedError,
    DeviceUnreachableError,
    DeviceWritesDisabledError,
    InvalidTokenError,
)
from hco_core.domain.apply import ApplyPlan, ApplyResult, ApplyStrategy
from hco_core.domain.config import ConfigFormat
from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.domain.secrets import DeviceCredentials
from hco_core.engine.remediation import build_bundle, parse, render_lines
from hco_core.engine.validation import validate_plan
from hco_core.ports import ConnectionDriver, DeviceSession

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TRANSCRIPTS = FIXTURES / "transcripts"
KEY = b"k" * 32
IOS = "CISCO_IOS"
LAST_CHANGE = "show running-config | include Last configuration change"
CREDENTIALS = DeviceCredentials(username="admin")


def device(device_id: str = "r1", platform_name: str = IOS) -> DeviceRecord:
    """Return a device record with the id and the platform."""
    return DeviceRecord(
        id=DeviceID(device_id),
        name=device_id,
        platform_name=platform_name,
        mgmt_address="10.0.0.1",
    )


async def in_session(record: DeviceRecord) -> DeviceSession:
    """Open a session to the device and return it."""
    async with MockConnectionDriver(TRANSCRIPTS, token_key=KEY).session(
        record, CREDENTIALS
    ) as session:
        return session


def test_the_driver_satisfies_the_port() -> None:
    """The driver is a `ConnectionDriver` for CLI on the table platforms."""
    driver = MockConnectionDriver(TRANSCRIPTS, token_key=KEY)
    assert isinstance(driver, ConnectionDriver)
    assert driver.name == "mock"
    assert driver.supports(IOS, ConfigFormat.CLI)
    assert not driver.supports(IOS, ConfigFormat.XML)
    assert not driver.supports("JUNIPER_JUNOS", ConfigFormat.CLI)
    with pytest.raises(ValueError, match="32 bytes"):
        MockConnectionDriver(TRANSCRIPTS, token_key=b"short")


def test_the_session_replays_the_transcripts() -> None:
    """Running config, fingerprint, and show output come from the transcripts."""
    session = asyncio.run(in_session(device()))
    assert isinstance(session, DeviceSession)
    running = asyncio.run(session.fetch_running())
    assert running.body.startswith("interface GigabitEthernet1/0/1\n")
    assert running.source == "device"
    fingerprint = asyncio.run(session.config_fingerprint())
    assert fingerprint is not None
    assert fingerprint.startswith("! Last configuration change at 10:00:00")
    assert running.fingerprint == fingerprint
    state = asyncio.run(session.collect_state(["show version", LAST_CHANGE]))
    assert state["show version"].startswith("Cisco IOS Software")
    assert set(state) == {"show version", LAST_CHANGE}
    dry_run = asyncio.run(
        session.dry_run(
            ApplyPlan(
                format=ConfigFormat.CLI, strategy=ApplyStrategy.IOS_ARCHIVE_REPLACE
            )
        )
    )
    assert not dry_run.supported


@pytest.mark.parametrize("command", ["show clock; reload", "configure terminal"])
def test_collect_state_refuses_a_command_outside_the_allowlist(command: str) -> None:
    """A command outside the allowlist is refused before any command runs."""
    session = asyncio.run(in_session(device()))
    with pytest.raises(CommandNotAllowedError, match=command.split(";", maxsplit=1)[0]):
        asyncio.run(session.collect_state(["show version", command]))


def test_an_allowed_command_without_a_transcript_is_unreachable() -> None:
    """An allowed command with no transcript file makes the device unreachable."""
    session = asyncio.run(in_session(device()))
    with pytest.raises(DeviceUnreachableError, match=r"show_users\.txt"):
        asyncio.run(session.collect_state(["show users"]))


def test_a_missing_transcript_makes_the_device_unreachable(tmp_path: Path) -> None:
    """A device without a transcript folder is unreachable."""
    with pytest.raises(DeviceUnreachableError, match="nope"):
        asyncio.run(in_session(device("nope")))
    (tmp_path / "r9").mkdir()
    driver = MockConnectionDriver(tmp_path, token_key=KEY)

    async def fetch() -> None:
        async with driver.session(device("r9"), CREDENTIALS) as session:
            assert await session.config_fingerprint() is None
            await session.fetch_running()

    with pytest.raises(DeviceUnreachableError, match=r"running\.txt"):
        asyncio.run(fetch())


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (device("../r1"), "transcript folder"),
        (device(platform_name="JUNIPER_JUNOS"), "JUNIPER_JUNOS"),
    ],
)
def test_a_bad_device_is_refused(record: DeviceRecord, message: str) -> None:
    """A device id outside the folder, or a platform outside the table, is refused."""
    with pytest.raises(AdapterConfigError, match=message):
        asyncio.run(in_session(record))


def signed_plan() -> ApplyPlan:
    """Return the fixture remediation of r1, signed by the validation gate."""
    running = parse(
        IOS, (TRANSCRIPTS / "r1" / "running.txt").read_text(encoding="utf8")
    )
    intended = parse(
        IOS,
        (FIXTURES / "configs" / "cisco_ios" / "intended.txt").read_text(
            encoding="utf8"
        ),
    )
    lines = render_lines(IOS, build_bundle(IOS, running, intended).remediation_dump)
    outcome = validate_plan(
        lines, running, intended, platform_name=IOS, forbidden=(), key=KEY
    )
    assert outcome.token is not None
    return ApplyPlan(
        format=ConfigFormat.CLI,
        strategy=ApplyStrategy.IOS_ARCHIVE_REPLACE,
        lines=lines,
        validation_token=outcome.token,
    )


def test_apply_verifies_the_token_then_refuses_to_write() -> None:
    """A signed plan reaches the write refusal; a tampered plan stops at the token."""
    session = asyncio.run(in_session(device()))
    plan = signed_plan()
    with pytest.raises(DeviceWritesDisabledError):
        asyncio.run(session.apply(plan))
    tampered = (
        plan.model_copy(update={"lines": (*plan.lines, "no ip routing")}),
        plan.model_copy(update={"validation_token": ""}),
    )
    for bad in tampered:
        with pytest.raises(InvalidTokenError):
            asyncio.run(session.apply(bad))


def test_confirm_and_rollback_refuse_to_write() -> None:
    """No change exists to confirm or roll back."""
    session = asyncio.run(in_session(device()))
    result = ApplyResult(succeeded=False, applied_at=datetime.now(UTC))
    for call in (session.confirm(result), session.rollback(result)):
        with pytest.raises(DeviceWritesDisabledError):
            asyncio.run(call)


def test_apply_refuses_a_token_for_another_running_config_platform_or_key(
    tmp_path: Path,
) -> None:
    """The token signs the running config, the platform, and the key."""
    plan = signed_plan()
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "running.txt").write_text("hostname other\n", encoding="utf8")
    eos = tmp_path / "e1"
    eos.mkdir()
    running = (TRANSCRIPTS / "r1" / "running.txt").read_text(encoding="utf8")
    (eos / "running.txt").write_text(running, encoding="utf8")
    sessions = (
        (tmp_path, KEY, device()),
        (tmp_path, KEY, device("e1", "ARISTA_EOS")),
        (TRANSCRIPTS, b"o" * 32, device()),
    )
    for folder, key, record in sessions:

        async def apply(
            folder: Path = folder, key: bytes = key, record: DeviceRecord = record
        ) -> None:
            async with MockConnectionDriver(folder, token_key=key).session(
                record, CREDENTIALS
            ) as session:
                await session.apply(plan)

        with pytest.raises(InvalidTokenError):
            asyncio.run(apply())


def test_a_transcript_that_is_not_utf8_is_unreachable(tmp_path: Path) -> None:
    """Bytes that are not UTF-8 in a transcript make the device unreachable."""
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "running.txt").write_bytes(b"\xff\xfe")

    async def fetch() -> None:
        async with MockConnectionDriver(tmp_path, token_key=KEY).session(
            device(), CREDENTIALS
        ) as session:
            await session.fetch_running()

    with pytest.raises(DeviceUnreachableError, match=r"running\.txt"):
        asyncio.run(fetch())
