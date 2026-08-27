"""Applying a change to a device, and undoing it."""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from hco_core.domain.base import BaseModel
from hco_core.domain.config import ConfigFormat


class ApplyStrategy(StrEnum):
    """How a change is applied so that it can be undone.

    Ordered by strength. The first four revert **without HCO** once armed, which
    is the property that matters: a device-side timer survives the orchestrator
    crashing, the network partitioning, and the change itself cutting the path
    back to the device.

    `HCO_TIMED_REVERT` survives none of those and is the fallback of last
    resort.
    """

    IOSXR_COMMIT_CONFIRMED = "iosxr_commit_confirmed"
    EOS_SESSION_COMMIT_TIMER = "eos_session_commit_timer"
    JUNOS_COMMIT_CONFIRMED = "junos_commit_confirmed"
    NETCONF_CONFIRMED_COMMIT = "netconf_confirmed_commit"
    NXOS_CHECKPOINT = "nxos_checkpoint"
    IOS_ARCHIVE_REPLACE = "ios_archive_replace"
    HCO_TIMED_REVERT = "hco_timed_revert"

    @property
    def reverts_without_orchestrator(self) -> bool:
        """True when the device undoes the change on its own if not confirmed.

        Only strategies where this is true can take part in a strict
        all-or-nothing change set, because aborting one must not require
        reaching every device.
        """
        return self in {
            ApplyStrategy.IOSXR_COMMIT_CONFIRMED,
            ApplyStrategy.EOS_SESSION_COMMIT_TIMER,
            ApplyStrategy.JUNOS_COMMIT_CONFIRMED,
            ApplyStrategy.NETCONF_CONFIRMED_COMMIT,
        }


class ApplyPlan(BaseModel):
    """Exactly what will be sent to one device."""

    format: ConfigFormat
    strategy: ApplyStrategy
    lines: tuple[str, ...] = ()
    payload: str | None = None
    updates: Mapping[str, Any] | None = None
    deletes: tuple[str, ...] = ()
    confirm_seconds: int | None = None
    rollback_lines: tuple[str, ...] = ()
    validation_token: str = ""
    """Proof that this exact plan passed validation.

    A session refuses a plan whose token does not verify. This is the mechanism
    that makes "every device write passes exactly one gate" enforceable rather
    than a convention: a plan constructed anywhere else cannot be applied.
    """


class ApplyResult(BaseModel):
    """What happened, and how to confirm or undo it."""

    succeeded: bool
    applied_at: datetime
    transcript: str = ""
    confirm_handle: str | None = None
    """Present when a device-side revert timer is armed and running.

    While it is set, prefer letting the timer expire over sending an explicit
    rollback: expiry is safer than adding traffic to a device that may be
    unhealthy, and it does not require reaching a device that may now be
    unreachable because of the change.
    """
    rollback_handle: str | None = None
    error: str | None = None


class DryRunResult(BaseModel):
    """What a device says a change would do, without doing it."""

    supported: bool
    """False is a legitimate answer, not an error. Cisco IOS has no dry run."""
    would_succeed: bool | None = None
    diff: str = ""
    errors: tuple[str, ...] = ()
