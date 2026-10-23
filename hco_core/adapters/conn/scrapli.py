"""A connection driver that reads from a device over SSH with scrapli.

The driver needs the `scrapli` extra: `pip install hco-core[scrapli]`. It uses
the synchronous `Scrapli` driver with the system transport, so the `ssh` of
the host makes the connection, and every scrapli call runs in a worker thread.
The transport checks the host key, so the key of the device must be in the
known hosts of the user that runs hco-core.

Every answer of the device is untrusted input. scrapli reads the whole answer
first, and `timeout_ops` bounds that read. The session then refuses an answer
above `MAX_OUTPUT_CHARS` and never keeps it, and it never quotes a command
answer in an error. The text of a scrapli error can hold the device prompt.
The device writes of Phase 4 are disabled: `apply` verifies the validation
token, then refuses.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import MappingProxyType

import structlog
from pydantic import SecretStr
from scrapli import Scrapli
from scrapli.driver.network import NetworkDriver
from scrapli.exceptions import ScrapliException

from hco_core.adapters.errors import (
    AdapterConfigError,
    CommandNotAllowedError,
    DeviceUnreachableError,
    DeviceWritesDisabledError,
    InvalidTokenError,
)
from hco_core.domain.apply import ApplyPlan, ApplyResult, DryRunResult
from hco_core.domain.config import ConfigDocument, ConfigFormat
from hco_core.domain.device import DeviceRecord
from hco_core.domain.secrets import DeviceCredentials
from hco_core.engine.platforms import PLATFORMS, PlatformProfile
from hco_core.engine.remediation import parse
from hco_core.engine.validation import MIN_KEY_BYTES, verify_token
from hco_core.ports.connection import DeviceSession

SCRAPLI_PLATFORMS: Mapping[str, str] = MappingProxyType(
    {
        "CISCO_IOS": "cisco_iosxe",
        "CISCO_NXOS": "cisco_nxos",
        "CISCO_XR": "cisco_iosxr",
        "ARISTA_EOS": "arista_eos",
    }
)
"""The scrapli core platform of each platform of the platform table."""
RUNNING_CONFIG_COMMAND = "show running-config"
MAX_OUTPUT_CHARS = 16 * 1024 * 1024
"""The most characters that one command may answer."""
SSH_PORT = 22
_MAX_REASON_CHARS = 200
_REDACTED = "***"
_log = structlog.get_logger(__name__)


class ScrapliConnectionDriver:
    """Open SSH sessions with scrapli to the platforms of `SCRAPLI_PLATFORMS`."""

    def __init__(self, *, token_key: bytes) -> None:
        """Use the key that verifies plan tokens.

        Raises:
            ValueError: If the key is shorter than `MIN_KEY_BYTES`.

        """
        if len(token_key) < MIN_KEY_BYTES:
            message = f"the token key must have at least {MIN_KEY_BYTES} bytes"
            raise ValueError(message)
        self._key = token_key

    @property
    def name(self) -> str:
        """The adapter name, `scrapli`."""
        return "scrapli"

    def supports(self, platform_name: str, fmt: ConfigFormat) -> bool:  # ruff: ignore[no-self-use]
        """Return True for CLI on a platform with a scrapli core driver."""
        return platform_name in SCRAPLI_PLATFORMS and fmt == ConfigFormat.CLI

    @asynccontextmanager
    async def session(
        self,
        device: DeviceRecord,
        credentials: DeviceCredentials,
        *,
        timeout: float = 60.0,  # ruff: ignore[async-function-with-timeout]
    ) -> AsyncGenerator[DeviceSession]:
        """Open an SSH session to the management address of the device.

        The timeout applies to the connect, to the transport, and to each
        command. A connection that scrapli cannot open raises
        `DeviceUnreachableError`.

        Yields:
            A session that reads from the device.

        Raises:
            AdapterConfigError: For an unsupported platform or format, or a
                management address that is not a host name or an IP address.

        """
        if not self.supports(device.platform_name, device.config_format):
            message = f"the scrapli driver does not support {device.platform_name} in {device.config_format}"
            raise AdapterConfigError(message)
        if not _is_host(device.mgmt_address):
            message = f"the management address of device {device.id!r} is not a host name or an IP address"
            raise AdapterConfigError(message)
        connection = await asyncio.to_thread(_connect, device, credentials, timeout)
        try:
            yield ScrapliSession(device, credentials, connection, self._key)
        finally:
            with contextlib.suppress(ScrapliException, OSError):
                await asyncio.to_thread(connection.close)


class ScrapliSession:
    """A session that reads over one scrapli connection and refuses every write."""

    def __init__(
        self,
        device: DeviceRecord,
        credentials: DeviceCredentials,
        connection: NetworkDriver,
        key: bytes,
    ) -> None:
        """Read over the open connection. The credentials are kept for redaction only."""
        self._device = device
        self._credentials = credentials
        self._connection = connection
        self._key = key
        self._profile: PlatformProfile = PLATFORMS[device.platform_name]
        self._lock = asyncio.Lock()

    async def fetch_running(self) -> ConfigDocument:
        """Return the running config, with the fingerprint of the platform command."""
        return ConfigDocument(
            body=await self._send(RUNNING_CONFIG_COMMAND),
            collected_at=datetime.now(UTC),
            source="device",
            fingerprint=await self.config_fingerprint(),
        )

    async def config_fingerprint(self) -> str | None:
        """Return the stripped output of the fingerprint command of the platform."""
        output = await self._send(self._profile.fingerprint_command)
        return output.strip()

    async def collect_state(self, commands: Sequence[str]) -> Mapping[str, str]:
        """Return the output of each command.

        Raises:
            CommandNotAllowedError: If a command is not on the allowlist. No
                command is sent then.

        """
        allowed = {
            self._profile.fingerprint_command,
            *self._profile.baseline_commands,
            *(c for _, commands in self._profile.section_commands for c in commands),
        }
        for command in commands:
            if command not in allowed:
                message = f"{command!r} is not a read-only command of {self._device.platform_name}"
                raise CommandNotAllowedError(message)
        return {command: await self._send(command) for command in commands}

    async def dry_run(self, plan: ApplyPlan) -> DryRunResult:  # ruff: ignore[unused-method-argument, no-self-use]
        """Return an unsupported dry run."""
        return DryRunResult(supported=False)

    async def apply(self, plan: ApplyPlan) -> ApplyResult:
        """Verify the plan's token against the running config, then refuse the write.

        Raises:
            InvalidTokenError: If the token does not sign this plan.
            DeviceWritesDisabledError: Always, after the token verifies.

        """
        platform_name = self._device.platform_name
        running = parse(platform_name, await self._send(RUNNING_CONFIG_COMMAND))
        if not verify_token(
            plan.lines,
            plan.validation_token,
            self._key,
            platform_name=platform_name,
            running=running,
        ):
            message = "the plan has no valid validation token"
            raise InvalidTokenError(message)
        message = "device writes are disabled until Phase 4"
        raise DeviceWritesDisabledError(message)

    async def confirm(self, result: ApplyResult) -> None:  # ruff: ignore[unused-method-argument, no-self-use]
        """Refuse, because no write happened.

        Raises:
            DeviceWritesDisabledError: Always.

        """
        raise DeviceWritesDisabledError

    async def rollback(self, result: ApplyResult) -> None:  # ruff: ignore[unused-method-argument, no-self-use]
        """Refuse, because no write happened.

        Raises:
            DeviceWritesDisabledError: Always.

        """
        raise DeviceWritesDisabledError

    async def _send(self, command: str) -> str:
        """Send one command in a worker thread and return its output.

        One command runs at a time on the connection. A failure string of the
        scrapli platform counts only on the first line of the answer, so device
        text further down never decides if a read works.

        Raises:
            DeviceUnreachableError: On a transport error, a refused command, or
                an output above `MAX_OUTPUT_CHARS`. The message never quotes
                the output.

        """
        try:
            async with self._lock:
                response = await asyncio.to_thread(
                    self._connection.send_command, command, failed_when_contains=[]
                )
        except (ScrapliException, OSError) as error:
            message = _unreachable_message(
                self._device, self._credentials, f"did not answer {command!r}", error
            )
        else:
            if self._refused(response.result):
                message = f"device {self._device.id!r} refused {command!r}"
                raise DeviceUnreachableError(message)
            if len(response.result) > MAX_OUTPUT_CHARS:
                message = f"device {self._device.id!r} answered {command!r} with more than {MAX_OUTPUT_CHARS} characters"
                raise DeviceUnreachableError(message)
            return response.result
        raise DeviceUnreachableError(message)

    def _refused(self, answer: str) -> bool:
        """Return True if the first line with text is a CLI error of the platform."""
        first_line = next((line for line in answer.splitlines() if line.strip()), "")
        return any(text in first_line for text in self._connection.failed_when_contains)


def _connect(
    device: DeviceRecord, credentials: DeviceCredentials, timeout: float
) -> NetworkDriver:
    """Open a scrapli connection to the device, in the calling thread.

    Raises:
        DeviceUnreachableError: If scrapli cannot open the connection. The
            message never holds a credential.

    """
    connection = Scrapli(  # pyright: ignore[reportCallIssue]
        platform=SCRAPLI_PLATFORMS[device.platform_name],  # pyright: ignore[reportCallIssue]
        host=device.mgmt_address,
        port=SSH_PORT,
        transport="system",
        auth_username=credentials.username,
        auth_password=_value(credentials.password),
        auth_secondary=_value(credentials.enable),
        auth_strict_key=True,
        timeout_socket=timeout,
        timeout_transport=timeout,
        timeout_ops=timeout,
    )
    try:
        connection.open()
    except (ScrapliException, OSError) as error:
        message = _unreachable_message(device, credentials, "cannot be opened", error)
    else:
        return connection
    with contextlib.suppress(ScrapliException, OSError):
        connection.close()
    raise DeviceUnreachableError(message)


def _unreachable_message(
    device: DeviceRecord, credentials: DeviceCredentials, what: str, error: Exception
) -> str:
    """Log the error and return its message with every credential value redacted.

    The caller raises outside the `except` block, so the scrapli error is not
    chained to the new error and is not in a frame of its traceback.
    """
    reason = _redacted_reason(error, credentials)
    _log.warning("device_unreachable", device=device.id, reason=reason)
    return f"device {device.id!r} {what}: {reason}"


def _value(secret: SecretStr | None) -> str:
    """Return the value of the secret, or an empty string for None."""
    return "" if secret is None else secret.get_secret_value()


def _is_host(address: str) -> bool:
    """Return True if the address can be the destination argument of `ssh`."""
    return (
        bool(address)
        and not address.startswith("-")
        and address.isprintable()
        and " " not in address
    )


def _redacted_reason(error: Exception, credentials: DeviceCredentials) -> str:
    """Return the error type and text on one line, with each credential redacted."""
    name = type(error).__name__
    text = f"{name}: {error}" if str(error) else name
    values = (
        credentials.username,
        *(
            _value(s)
            for s in (credentials.password, credentials.enable, credentials.private_key)
        ),
    )
    for value in values:
        if value:
            text = text.replace(value, _REDACTED)
    one_line = "".join(c if c.isprintable() else " " for c in text)
    return one_line[:_MAX_REASON_CHARS]
