"""Configuration as collected from a device or a repository."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from hco_core.domain.base import BaseModel


class ConfigFormat(StrEnum):
    """How a device expresses configuration.

    `hier_config` 4.x parses and renders all three, and the apply path differs
    for each: CLI takes lines, NETCONF takes an edit-config payload from
    `WorkflowRemediation.remediation_netconf_xml()`, and gNMI takes a SetRequest
    dict from `remediation_json()`.

    A NETCONF device is never handed CLI text.
    """

    CLI = "cli"
    XML = "xml"
    JSON = "json"


class ConfigDocument(BaseModel):
    """Configuration as collected, before parsing.

    `body` is the faithful original. It is never redacted and never normalised,
    because audit and rollback need exactly what the device said. Redaction
    happens on the way into a prompt, never on the way into storage.
    """

    body: str
    format: ConfigFormat = ConfigFormat.CLI
    collected_at: datetime
    source: Literal["device", "git", "sot", "upload"]
    fingerprint: str | None = None
    """A cheap device-side indicator that configuration changed since collection.

    Used to detect a stale repository-sourced config before apply. `None` when
    the platform offers nothing cheap, which callers must treat as "assume
    stale" rather than "assume fresh".
    """
