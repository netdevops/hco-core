"""Device access.

Format-aware, with confirm and rollback as protocol members rather than
something each caller improvises. Safe apply is too important to leave to
convention.
"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable

from hco_core.domain.apply import ApplyPlan, ApplyResult, DryRunResult
from hco_core.domain.config import ConfigDocument, ConfigFormat
from hco_core.domain.device import DeviceRecord
from hco_core.domain.secrets import DeviceCredentials


@runtime_checkable
class DeviceSession(Protocol):
    """An open session to one device.

    **Everything this returns is untrusted.** Running configuration, show output,
    banners, interface descriptions, ACL remarks, and hostnames are all
    attacker-influenced by anyone with configuration access to the device.
    """

    async def fetch_running(self) -> ConfigDocument:
        """Collect the running configuration."""
        ...

    async def config_fingerprint(self) -> str | None:
        """Return a cheap indicator that configuration changed since collection.

        `None` when the platform offers nothing cheap. Callers must treat that
        as "assume stale", because the cost of re-collecting is one session and
        the cost of being wrong is an outage.
        """
        ...

    async def collect_state(self, commands: Sequence[str]) -> Mapping[str, str]:
        """Run read-only commands and return their output.

        An implementation must refuse any command not on the platform's
        read-only allowlist. A "read-only" command that is quietly mutating is
        exactly the mistake this guards against.
        """
        ...

    async def dry_run(self, plan: ApplyPlan) -> DryRunResult:
        """Validate without committing, where the platform supports it."""
        ...

    async def apply(self, plan: ApplyPlan) -> ApplyResult:
        """Apply, leaving the change pending confirmation where possible.

        On a platform with a native confirm timer this arms it and returns a
        `confirm_handle`: the change is live but reverts on its own unless
        confirmed. That property is what makes a strict all-or-nothing change
        set possible across devices.

        An implementation MUST verify `plan.validation_token` and refuse a plan
        that does not carry a valid one.
        """
        ...

    async def confirm(self, result: ApplyResult) -> None:
        """Make an applied change permanent."""
        ...

    async def rollback(self, result: ApplyResult) -> None:
        """Explicitly revert. The fallback, not the preference.

        Prefer letting a confirm timer expire: expiry survives the orchestrator
        crashing and the network partitioning, and it does not require reaching
        a device that may now be unreachable because of the change.
        """
        ...


@runtime_checkable
class ConnectionDriver(Protocol):
    """Opens sessions. One driver per transport library."""

    @property
    def name(self) -> str:
        """Which driver this is."""
        ...

    def supports(self, platform_name: str, fmt: ConfigFormat) -> bool:
        """Whether this driver can talk to that platform in that format."""
        ...

    def session(
        self,
        device: DeviceRecord,
        credentials: DeviceCredentials,
        *,
        timeout: float = 60.0,
    ) -> AbstractAsyncContextManager[DeviceSession]:
        """Open a session as an async context manager."""
        ...
