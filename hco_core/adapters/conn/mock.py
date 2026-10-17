"""A connection driver that replays transcripts, for development and tests."""

import asyncio
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

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

_NOT_FILE_CHARACTERS = re.compile(r"[^a-z0-9]+")
_RUNNING = "running.txt"


class MockConnectionDriver:
    """Open sessions that read `<transcripts_dir>/<device id>/` files.

    The folder holds `running.txt` and one file for each command, named from
    the command in lower case with each run of other characters as `_`, such
    as `show_version.txt`.
    """

    def __init__(self, transcripts_dir: Path, *, token_key: bytes) -> None:
        """Use the transcripts folder, and the key that verifies plan tokens.

        Raises:
            ValueError: If the key is shorter than `MIN_KEY_BYTES`.

        """
        if len(token_key) < MIN_KEY_BYTES:
            message = f"the token key must have at least {MIN_KEY_BYTES} bytes"
            raise ValueError(message)
        self._root = transcripts_dir.resolve()
        self._key = token_key

    @property
    def name(self) -> str:
        """The adapter name, `mock`."""
        return "mock"

    def supports(self, platform_name: str, fmt: ConfigFormat) -> bool:  # ruff: ignore[no-self-use]
        """Return True for CLI on a platform of the platform table."""
        return platform_name in PLATFORMS and fmt == ConfigFormat.CLI

    @asynccontextmanager
    async def session(
        self,
        device: DeviceRecord,
        credentials: DeviceCredentials,  # ruff: ignore[unused-method-argument]
        *,
        timeout: float = 60.0,  # ruff: ignore[unused-method-argument, async-function-with-timeout]
    ) -> AsyncGenerator[DeviceSession]:
        """Open a session to the device's transcript folder.

        Yields:
            A session that reads the device's transcripts.

        Raises:
            AdapterConfigError: For an unsupported platform or a bad device id.
            DeviceUnreachableError: If the device has no transcript folder.

        """
        if not self.supports(device.platform_name, device.config_format):
            message = f"the mock driver does not support {device.platform_name} in {device.config_format}"
            raise AdapterConfigError(message)
        folder = (self._root / device.id).resolve()
        if folder.parent != self._root:
            message = f"the device id {device.id!r} is not a transcript folder name"
            raise AdapterConfigError(message)
        if not await asyncio.to_thread(folder.is_dir):
            message = f"no transcript for device {device.id!r} in {self._root}"
            raise DeviceUnreachableError(message)
        yield MockSession(device, folder, self._key)


class MockSession:
    """A session that reads transcripts and refuses every write."""

    def __init__(self, device: DeviceRecord, folder: Path, key: bytes) -> None:
        """Read the device's transcripts from the folder."""
        self._device = device
        self._folder = folder
        self._key = key
        self._profile: PlatformProfile = PLATFORMS[device.platform_name]

    async def fetch_running(self) -> ConfigDocument:
        """Return `running.txt`, with the fingerprint of the platform command."""
        return ConfigDocument(
            body=await self._read(_RUNNING),
            collected_at=datetime.now(UTC),
            source="device",
            fingerprint=await self.config_fingerprint(),
        )

    async def config_fingerprint(self) -> str | None:
        """Return the output of the fingerprint command, or None without a transcript."""
        try:
            output = await self._read(_file_name(self._profile.fingerprint_command))
        except DeviceUnreachableError:
            return None
        return output.strip()

    async def collect_state(self, commands: Sequence[str]) -> Mapping[str, str]:
        """Return the output of each command.

        An allowed command with no transcript raises `DeviceUnreachableError`.

        Raises:
            CommandNotAllowedError: If a command is not on the allowlist. No
                command runs then.

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
        return {command: await self._read(_file_name(command)) for command in commands}

    async def dry_run(self, plan: ApplyPlan) -> DryRunResult:  # ruff: ignore[unused-method-argument, no-self-use]
        """Return an unsupported dry run."""
        return DryRunResult(supported=False)

    async def apply(self, plan: ApplyPlan) -> ApplyResult:
        """Verify the plan's token against `running.txt`, then refuse the write.

        Raises:
            InvalidTokenError: If the token does not sign this plan.
            DeviceWritesDisabledError: Always, after the token verifies.

        """
        platform_name = self._device.platform_name
        running = parse(platform_name, await self._read(_RUNNING))
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

    async def _read(self, name: str) -> str:
        path = self._folder / name
        try:
            return await asyncio.to_thread(path.read_text, encoding="utf8")
        except (OSError, UnicodeDecodeError) as error:
            message = f"no UTF-8 transcript {name} for device {self._device.id!r}"
            raise DeviceUnreachableError(message) from error


def _file_name(command: str) -> str:
    return f"{_NOT_FILE_CHARACTERS.sub('_', command.lower()).strip('_')}.txt"
