"""Risk assessment: the shape of the judgement, not the judgement itself.

These models are published so that anyone can implement a `RemediationPlanner`
and so that a customer can read what an analyser is allowed to say. The values
that populate them -- the thresholds that set a floor, the prompts that elicit
findings, the labelled data that measures whether an assessment was any good --
are not part of this package.

An interface is not a moat.
"""

from collections.abc import Mapping
from enum import IntEnum
from typing import Any, Literal

from pydantic import Field

from hco_core.domain.base import BaseModel
from hco_core.domain.ids import DeviceID


class RiskTier(IntEnum):
    """How much care a change needs.

    `IntEnum` deliberately: tiers must be comparable and combinable with `max()`,
    because the reconciliation rule is that a model may raise a tier and may
    never lower one.
    """

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


class ImpactFinding(BaseModel):
    """One thing that could go wrong."""

    category: Literal[
        "reachability",
        "routing",
        "security",
        "capacity",
        "availability",
        "compliance",
        "operational",
        "unknown",
    ]
    severity: Literal["info", "low", "medium", "high", "critical"]
    summary: str = Field(max_length=280)
    detail: str = Field(default="", max_length=2000)
    affected_commands: tuple[str, ...] = ()
    affected_objects: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    """Which evidence keys support this finding.

    A finding that cites nothing is a finding the model invented. Requiring a
    citation makes that visible in review and scoreable in an evaluation.
    """
    likelihood: Literal["certain", "likely", "possible", "unlikely"] = "possible"
    mitigations: tuple[str, ...] = ()


class RiskAssessment(BaseModel):
    """An analyser's verdict on a change.

    **There is no field for commands.** The schema is the enforcement: an
    analyser assesses consequences and does not write configuration.
    """

    tier: RiskTier
    confidence: Literal["high", "medium", "low"]
    summary: str = Field(max_length=1000)
    findings: tuple[ImpactFinding, ...] = ()
    required_verifications: tuple[str, ...] = ()
    """Post-change checks this change must pass.

    An implementation may ADD checks. It can never remove one.
    """
    unknowns: tuple[str, ...] = ()
    """What the analyser could not determine.

    Populating this should cap confidence and may escalate the tier. Saying "I
    do not know" must be cheap, or an analyser learns to guess.
    """
    rollback_notes: str = ""
    traffic_impacting: bool = False
    requires_maintenance_window: bool = False
    suggested_canary: tuple[DeviceID, ...] = ()
    """Advisory only. Canary selection decides which customers are exposed to a
    bad change first, which is a policy decision from structured metadata rather
    than a judgement call."""


class RiskEvidence(BaseModel):
    """The facts an analyser is given.

    Trust is not uniform across these fields, and an implementation must treat
    them differently. See the `RemediationPlanner` invariants.
    """

    device: Mapping[str, Any] = Field(default_factory=dict)
    topology: Mapping[str, Any] = Field(default_factory=dict)
    config_derived: Mapping[str, Any] = Field(default_factory=dict)
    future_report: Mapping[str, Any] = Field(default_factory=dict)
    """`unresolved_negations` and `idempotency_replacements` from
    `HConfig.future_with_report()`. Structured, deterministic, and the highest
    signal available: an unresolved negation means the remediation will not do
    what it claims."""
    retrieved: tuple[Mapping[str, Any], ...] = ()
    live_state: Mapping[str, str] = Field(default_factory=dict)
    """UNTRUSTED. Raw device output. An interface description is
    attacker-controllable by anyone with configuration access to any polled
    device."""
    telemetry: Mapping[str, Any] = Field(default_factory=dict)
    """UNTRUSTED. Alert labels and annotations are operator-authored free text."""

    model_config = BaseModel.model_config | {"arbitrary_types_allowed": False}


class ChangeSet(BaseModel):
    """One or more device changes that succeed or fail together."""

    id: str
    title: str = ""
    description: str = ""
    device_ids: tuple[DeviceID, ...] = ()
    platform_name: str = ""
    remediation_dump: Mapping[str, Any] = Field(default_factory=dict)
    """The remediation as `HConfig.dump()` JSON, never rendered text.

    Rendering depends on sort order and indentation and it drops tags, so it is
    lossy across a process boundary.
    """
    rollback_dump: Mapping[str, Any] = Field(default_factory=dict)
    plan_source: Literal["deterministic", "ai", "manual"] = "deterministic"
