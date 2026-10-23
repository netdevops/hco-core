"""The scrapli connection reads over SSH and never writes to a device."""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

pytest.importorskip("scrapli")

from scrapli.exceptions import (
    ScrapliAuthenticationFailed,
    ScrapliConnectionNotOpened,
    ScrapliException,
    ScrapliTimeout,
)
from scrapli.response import Response
from structlog.testing import capture_logs

from hco_core.adapters.conn import scrapli as scrapli_adapter
from hco_core.adapters.conn.scrapli import SCRAPLI_PLATFORMS, ScrapliConnectionDriver
from hco_core.adapters.errors import (
    AdapterConfigError,
    CommandNotAllowedError,
    DeviceUnreachableError,
    DeviceWritesDisabledError,
    InvalidTokenError,
)
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.domain.apply import ApplyPlan, ApplyResult, ApplyStrategy
from hco_core.domain.config import ConfigFormat
from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.domain.secrets import DeviceCredentials, SecretRef
from hco_core.engine.platforms import PLATFORMS
from hco_core.engine.remediation import build_bundle, parse, render_lines
from hco_core.engine.validation import validate_plan
from hco_core.ports import ConnectionDriver, DeviceSession

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TRANSCRIPTS = FIXTURES / "transcripts" / "r1"
KEY = b"k" * 32
IOS = "CISCO_IOS"
USERNAME = "adm1n-USER-marker"
PASSWORD = "p4ss-SECRET-marker"  # ruff: ignore[hardcoded-password-string]
ENABLE = "en4ble-SECRET-marker"
CREDENTIALS = DeviceCredentials(
    username=USERNAME, password=SecretStr(PASSWORD), enable=SecretStr(ENABLE)
)
INVALID_INPUT = "% Invalid input detected at '^' marker."
RUNNING = (TRANSCRIPTS / "running.txt").read_text(encoding="utf8")
LAST_CHANGE = "! Last configuration change at 10:00:00 UTC Fri Oct 16 2026 by admin"
VERSION = "Cisco IOS Software, Catalyst L3 Switch Software\n"
TIMEOUT = 12.5
"""A session timeout that differs from every scrapli default."""
DEFAULT_TIMEOUT = 60.0
"""The session timeout of the port when the caller gives none."""
CALLS_IN_ONE_COLLECT = 3
"""Open, one command, and close."""
COMMANDS_AT_ONCE = 3
"""The concurrent awaits of the serialisation test."""


def device(platform_name: str = IOS) -> DeviceRecord:
    """Return a device record on the platform."""
    return DeviceRecord(
        id=DeviceID("r1"),
        name="r1",
        platform_name=platform_name,
        mgmt_address="10.0.0.1",
    )


def outputs(platform_name: str = IOS) -> dict[str, str]:
    """Return the answers of the device to the running config and show commands."""
    return {
        "show running-config": RUNNING,
        PLATFORMS[platform_name].fingerprint_command: f"{LAST_CHANGE}\n",
        "show version": VERSION,
        "show clock": "10:01:00 UTC Fri Oct 16 2026\n",
    }


@dataclass
class FakeConnection:
    """Stand in for one `Scrapli` connection, and record each call and its thread."""

    transport: "FakeTransport"
    kwargs: dict[str, object]
    failed_when_contains: list[str] = field(
        default_factory=lambda: ["% Ambiguous command", "% Invalid input detected"]
    )
    sent: list[str] = field(default_factory=list[str])
    send_kwargs: list[dict[str, object]] = field(
        default_factory=list[dict[str, object]]
    )
    threads: list[threading.Thread] = field(default_factory=list[threading.Thread])
    in_flight: int = 0
    most_in_flight: int = 0
    opened: bool = False
    closed: bool = False

    def open(self) -> None:
        """Open, or raise the open error of the transport."""
        self.threads.append(threading.current_thread())
        if self.transport.open_error is not None:
            raise self.transport.open_error
        self.opened = True

    def close(self) -> None:
        """Close, or raise the close error of the transport."""
        self.threads.append(threading.current_thread())
        self.closed = True
        if self.transport.close_error is not None:
            raise self.transport.close_error

    def send_command(
        self, command: str, *, failed_when_contains: list[str] | None = None
    ) -> Response:
        """Answer from the table, or raise the failure of the command.

        The call takes a moment, so two calls at one time overlap and
        `most_in_flight` records it.
        """
        self.threads.append(threading.current_thread())
        self.sent.append(command)
        self.send_kwargs.append({"failed_when_contains": failed_when_contains})
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        time.sleep(0.01)
        self.in_flight -= 1
        if command in self.transport.failures:
            raise self.transport.failures[command]
        response = Response(
            host="10.0.0.1",
            channel_input=command,
            failed_when_contains=(
                self.failed_when_contains
                if failed_when_contains is None
                else failed_when_contains
            ),
        )
        response.record_response(self.transport.outputs[command].encode())
        return response


@dataclass
class FakeTransport:
    """Stand in for the `Scrapli` factory. Each call makes one `FakeConnection`."""

    outputs: dict[str, str] = field(default_factory=outputs)
    failures: dict[str, Exception] = field(default_factory=dict[str, Exception])
    open_error: Exception | None = None
    close_error: Exception | None = None
    connections: list[FakeConnection] = field(default_factory=list[FakeConnection])

    def __call__(self, **kwargs: object) -> FakeConnection:
        """Make a connection with the arguments."""
        connection = FakeConnection(self, kwargs)
        self.connections.append(connection)
        return connection

    @property
    def connection(self) -> FakeConnection:
        """The one connection that the session opened."""
        assert len(self.connections) == 1
        return self.connections[0]


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """Replace `Scrapli` in the adapter with the fake transport."""
    fake = FakeTransport()
    monkeypatch.setattr(scrapli_adapter, "Scrapli", fake)
    return fake


def driver() -> ScrapliConnectionDriver:
    """Return a driver with the test key."""
    return ScrapliConnectionDriver(token_key=KEY)


def in_session[T](
    record: DeviceRecord,
    action: Callable[[DeviceSession], Awaitable[T]],
    credentials: DeviceCredentials = CREDENTIALS,
) -> T:
    """Open a session to the device, run the action in it, and return its result."""

    async def run() -> T:
        async with driver().session(record, credentials, timeout=TIMEOUT) as session:
            return await action(session)

    return asyncio.run(run())


def signed_plan() -> ApplyPlan:
    """Return the fixture remediation of r1, signed by the validation gate."""
    running = parse(IOS, RUNNING)
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


def test_the_driver_satisfies_the_port() -> None:
    """The driver is a `ConnectionDriver` for CLI on the four scrapli platforms."""
    assert isinstance(driver(), ConnectionDriver)
    assert driver().name == "scrapli"
    assert set(SCRAPLI_PLATFORMS) == {
        "CISCO_IOS",
        "CISCO_NXOS",
        "CISCO_XR",
        "ARISTA_EOS",
    }
    for platform_name in SCRAPLI_PLATFORMS:
        assert driver().supports(platform_name, ConfigFormat.CLI)
        assert not driver().supports(platform_name, ConfigFormat.XML)
    assert not driver().supports("JUNIPER_JUNOS", ConfigFormat.CLI)
    with pytest.raises(ValueError, match="32 bytes"):
        ScrapliConnectionDriver(token_key=b"short")


@pytest.mark.parametrize(
    ("platform_name", "scrapli_platform"),
    [
        ("CISCO_IOS", "cisco_iosxe"),
        ("CISCO_NXOS", "cisco_nxos"),
        ("CISCO_XR", "cisco_iosxr"),
        ("ARISTA_EOS", "arista_eos"),
    ],
)
def test_the_session_opens_scrapli_with_the_system_transport(
    transport: FakeTransport, platform_name: str, scrapli_platform: str
) -> None:
    """The session maps the platform, passes the credentials, and closes at the end."""
    transport.outputs = outputs(platform_name)

    in_session(device(platform_name), lambda session: session.config_fingerprint())

    connection = transport.connection
    assert connection.kwargs == {
        "platform": scrapli_platform,
        "host": "10.0.0.1",
        "port": 22,
        "transport": "system",
        "auth_username": USERNAME,
        "auth_password": PASSWORD,
        "auth_secondary": ENABLE,
        "auth_strict_key": True,
        "timeout_socket": TIMEOUT,
        "timeout_transport": TIMEOUT,
        "timeout_ops": TIMEOUT,
    }
    assert connection.opened
    assert connection.closed


def test_the_session_satisfies_the_port(transport: FakeTransport) -> None:
    """The open session is a `DeviceSession`."""

    async def open_session() -> object:
        async with driver().session(device(), CREDENTIALS) as session:
            return session

    assert isinstance(asyncio.run(open_session()), DeviceSession)
    assert transport.connection.kwargs["timeout_ops"] == pytest.approx(DEFAULT_TIMEOUT)


def test_a_credential_without_a_password_passes_empty_strings(
    transport: FakeTransport,
) -> None:
    """A missing secret is an empty string for scrapli, never None."""
    in_session(
        device(),
        lambda session: session.config_fingerprint(),
        DeviceCredentials(username="admin"),
    )

    kwargs = transport.connection.kwargs
    assert (kwargs["auth_password"], kwargs["auth_secondary"]) == ("", "")


def test_the_env_enable_secret_goes_to_scrapli_as_auth_secondary(
    transport: FakeTransport,
) -> None:
    """`HCO_SECRET_<KEY>_ENABLE` reaches scrapli, so the session can enter enable mode."""
    provider = EnvSecretsProvider(
        {
            "HCO_SECRET_R1_USERNAME": USERNAME,
            "HCO_SECRET_R1_PASSWORD": PASSWORD,
            "HCO_SECRET_R1_ENABLE": ENABLE,
        }
    )
    credentials = asyncio.run(provider.resolve(SecretRef(provider="env", key="r1")))

    in_session(device(), lambda session: session.config_fingerprint(), credentials)

    assert transport.connection.kwargs["auth_secondary"] == ENABLE


def test_fetch_running_returns_the_running_config(transport: FakeTransport) -> None:
    """The body is the answer to `show running-config`, with the fingerprint."""
    running = in_session(device(), lambda session: session.fetch_running())

    assert running.body == RUNNING
    assert running.source == "device"
    assert running.format == ConfigFormat.CLI
    assert running.fingerprint == LAST_CHANGE
    assert transport.connection.sent == [
        "show running-config",
        PLATFORMS[IOS].fingerprint_command,
    ]


@pytest.mark.parametrize("platform_name", list(SCRAPLI_PLATFORMS))
def test_config_fingerprint_sends_the_platform_command(
    transport: FakeTransport, platform_name: str
) -> None:
    """The fingerprint is the stripped answer to the command of the platform table."""
    transport.outputs = outputs(platform_name)

    fingerprint = in_session(
        device(platform_name), lambda session: session.config_fingerprint()
    )

    assert fingerprint == LAST_CHANGE
    assert transport.connection.sent == [PLATFORMS[platform_name].fingerprint_command]


def test_collect_state_returns_the_output_of_each_command(
    transport: FakeTransport,
) -> None:
    """Each allowed command is sent once, and its answer is returned as is."""
    state = in_session(
        device(), lambda session: session.collect_state(["show version", "show clock"])
    )

    assert state == {
        "show version": VERSION,
        "show clock": "10:01:00 UTC Fri Oct 16 2026\n",
    }
    assert transport.connection.sent == ["show version", "show clock"]


@pytest.mark.parametrize(
    "command", ["show clock; reload", "configure terminal", "show running-config"]
)
def test_collect_state_refuses_a_command_outside_the_allowlist(
    transport: FakeTransport, command: str
) -> None:
    """A command outside the allowlist is refused before any command is sent."""
    with pytest.raises(CommandNotAllowedError, match=command.split(";", maxsplit=1)[0]):
        in_session(
            device(), lambda session: session.collect_state(["show version", command])
        )

    assert transport.connection.sent == []


def test_every_scrapli_call_runs_in_a_worker_thread(transport: FakeTransport) -> None:
    """Open, each command, and close run under `asyncio.to_thread`."""
    in_session(device(), lambda session: session.collect_state(["show version"]))

    threads = transport.connection.threads
    assert len(threads) == CALLS_IN_ONE_COLLECT
    assert all(thread is not threading.main_thread() for thread in threads)


def test_dry_run_is_not_supported(transport: FakeTransport) -> None:
    """No scrapli platform has a dry run in this phase."""
    plan = ApplyPlan(
        format=ConfigFormat.CLI, strategy=ApplyStrategy.IOS_ARCHIVE_REPLACE
    )

    result = in_session(device(), lambda session: session.dry_run(plan))

    assert not result.supported
    assert transport.connection.sent == []


def test_apply_verifies_the_token_then_refuses_to_write(
    transport: FakeTransport,
) -> None:
    """A signed plan reaches the write refusal; a tampered plan stops at the token."""
    plan = signed_plan()

    with pytest.raises(DeviceWritesDisabledError):
        in_session(device(), lambda session: session.apply(plan))
    assert transport.connection.sent == ["show running-config"]

    tampered = (
        plan.model_copy(update={"lines": (*plan.lines, "no ip routing")}),
        plan.model_copy(update={"validation_token": ""}),
    )
    for bad in tampered:
        with pytest.raises(InvalidTokenError):
            in_session(device(), lambda session, bad=bad: session.apply(bad))
    assert all(
        connection.sent == ["show running-config"]
        for connection in transport.connections
    )


def test_apply_refuses_a_token_for_another_running_config(
    transport: FakeTransport,
) -> None:
    """The token signs the running config that the device answers now."""
    plan = signed_plan()
    transport.outputs["show running-config"] = "hostname other\n"

    with pytest.raises(InvalidTokenError):
        in_session(device(), lambda session: session.apply(plan))


def test_confirm_and_rollback_refuse_to_write(transport: FakeTransport) -> None:
    """No change exists to confirm or roll back."""
    result = ApplyResult(succeeded=False, applied_at=datetime.now(UTC))

    calls: tuple[Callable[[DeviceSession], Awaitable[None]], ...] = (
        lambda session: session.confirm(result),
        lambda session: session.rollback(result),
    )
    for call in calls:
        with pytest.raises(DeviceWritesDisabledError):
            in_session(device(), call)
    assert all(connection.sent == [] for connection in transport.connections)


def test_an_unsupported_device_is_refused_before_scrapli_opens(
    transport: FakeTransport,
) -> None:
    """A platform or a format outside the mapping is a configuration error."""
    junos = device("JUNIPER_JUNOS")
    xml = device().model_copy(update={"config_format": ConfigFormat.XML})

    for record, text in ((junos, "JUNIPER_JUNOS"), (xml, "xml")):
        with pytest.raises(AdapterConfigError, match=text):
            in_session(record, lambda session: session.config_fingerprint())
    assert transport.connections == []


@pytest.mark.parametrize("address", ["", "-oProxyCommand=touch /tmp/x", "10.0.0.1 x"])
def test_an_address_that_is_not_a_host_is_refused_before_scrapli_opens(
    transport: FakeTransport, address: str
) -> None:
    """The address is the first argument of `ssh`, so an option or a space is refused."""
    record = device().model_copy(update={"mgmt_address": address})

    with pytest.raises(AdapterConfigError, match="management address"):
        in_session(record, lambda session: session.config_fingerprint())

    assert transport.connections == []


def test_concurrent_calls_on_one_session_run_one_at_a_time(
    transport: FakeTransport,
) -> None:
    """One scrapli connection takes one command at a time."""

    async def three_at_once(session: DeviceSession) -> None:
        await asyncio.gather(
            session.collect_state(["show version"]),
            session.config_fingerprint(),
            session.collect_state(["show clock"]),
        )

    in_session(device(), three_at_once)

    connection = transport.connection
    assert connection.most_in_flight == 1
    assert len(connection.sent) == COMMANDS_AT_ONCE


def test_a_failure_string_inside_the_answer_does_not_fail_the_read(
    transport: FakeTransport,
) -> None:
    """Device text in a description or a banner never decides if a read works."""
    body = f"interface Gi1\n description {INVALID_INPUT}\n"
    transport.outputs["show running-config"] = body
    transport.outputs["show version"] = f"Version 1\n{INVALID_INPUT}\n"

    running = in_session(device(), lambda session: session.fetch_running())
    state = in_session(
        device(), lambda session: session.collect_state(["show version"])
    )

    assert running.body == body
    assert state["show version"] == f"Version 1\n{INVALID_INPUT}\n"
    assert all(
        call_kwargs == {"failed_when_contains": []}
        for connection in transport.connections
        for call_kwargs in connection.send_kwargs
    )


@pytest.mark.parametrize("answer", [INVALID_INPUT, f"\n  {INVALID_INPUT}\nmore\n"])
def test_an_error_as_the_first_line_of_the_answer_is_a_refused_command(
    transport: FakeTransport, answer: str
) -> None:
    """The failure strings of the scrapli platform match the first line only."""
    transport.outputs["show version"] = answer

    with pytest.raises(DeviceUnreachableError, match="refused 'show version'") as error:
        in_session(device(), lambda session: session.collect_state(["show version"]))

    assert "marker" not in str(error.value)


def test_the_traceback_locals_hold_no_scrapli_error(transport: FakeTransport) -> None:
    """The scrapli error is not chained and is not in a frame of the traceback."""
    transport.failures["show version"] = ScrapliTimeout(PASSWORD)
    transport.open_error = None

    with pytest.raises(DeviceUnreachableError) as error:
        in_session(device(), lambda session: session.collect_state(["show version"]))

    transport.open_error = ScrapliAuthenticationFailed(PASSWORD)
    with pytest.raises(DeviceUnreachableError) as open_error:
        in_session(device(), lambda session: session.config_fingerprint())

    for raised in (error.value, open_error.value):
        traceback = raised.__traceback__
        while traceback is not None:
            for value in traceback.tb_frame.f_locals.values():
                assert not isinstance(value, ScrapliException)
            traceback = traceback.tb_next


def test_an_open_failure_is_unreachable_without_the_credentials(
    transport: FakeTransport,
) -> None:
    """The error names the device and the scrapli error, never a secret."""
    transport.open_error = ScrapliAuthenticationFailed(
        f"auth failed for {USERNAME} with {PASSWORD} and {ENABLE}"
    )

    with capture_logs() as lines, pytest.raises(DeviceUnreachableError) as error:
        in_session(device(), lambda session: session.config_fingerprint())

    text = f"{error.value!s} {error.value!r} {lines!r} {driver()!r}"
    assert "ScrapliAuthenticationFailed" in str(error.value)
    assert "'r1'" in str(error.value)
    assert PASSWORD not in text
    assert ENABLE not in text
    assert USERNAME not in text
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert [line["event"] for line in lines] == ["device_unreachable"]
    assert transport.connection.closed


@pytest.mark.parametrize(
    "failure",
    [
        ScrapliTimeout("timed out"),
        ScrapliConnectionNotOpened(),
        OSError("connection reset"),
    ],
)
def test_a_transport_error_on_a_command_is_unreachable(
    transport: FakeTransport, failure: Exception
) -> None:
    """A scrapli error or an OS error while a command runs is wrapped."""
    transport.failures["show version"] = failure

    with pytest.raises(DeviceUnreachableError, match=type(failure).__name__):
        in_session(device(), lambda session: session.collect_state(["show version"]))

    assert transport.connection.closed


def test_a_command_that_the_device_refuses_is_unreachable(
    transport: FakeTransport,
) -> None:
    """A failed scrapli response is an error that quotes no device text."""
    transport.outputs["show version"] = "% Invalid input detected SECRET-FROM-DEVICE\n"

    with pytest.raises(DeviceUnreachableError, match="show version") as error:
        in_session(device(), lambda session: session.collect_state(["show version"]))

    assert "SECRET-FROM-DEVICE" not in str(error.value)


def test_output_above_the_bound_is_unreachable(
    transport: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Device output is untrusted, so its size is bounded."""
    monkeypatch.setattr(scrapli_adapter, "MAX_OUTPUT_CHARS", 64)
    transport.outputs["show version"] = "x" * 65

    with pytest.raises(DeviceUnreachableError, match="64") as error:
        in_session(device(), lambda session: session.collect_state(["show version"]))
    in_session(device(), lambda session: session.collect_state(["show clock"]))

    assert "xxx" not in str(error.value)


def test_a_close_error_does_not_hide_the_result(transport: FakeTransport) -> None:
    """A scrapli error on close is dropped; the session result stands."""
    transport.close_error = ScrapliException("already closed")

    fingerprint = in_session(device(), lambda session: session.config_fingerprint())

    assert fingerprint == LAST_CHANGE
    assert transport.connection.closed
