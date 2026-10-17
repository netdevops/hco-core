"""A remediation, its rollback, and the facts about its future config."""

from collections.abc import Mapping
from typing import TypedDict

from hco_core.domain.base import BaseModel


class DumpLineJson(TypedDict):
    """One line of a `hier_config` `Dump`, as JSON."""

    depth: int
    text: str
    tags: list[str]
    comments: list[str]
    new_in_config: bool


class DumpJson(TypedDict):
    """A `hier_config` `Dump` as JSON: the form a config takes across boundaries."""

    lines: list[DumpLineJson]


class NegationFact(BaseModel):
    """One negation line in the future config, with its place in the tree."""

    text: str
    path: tuple[str, ...]
    """The text of each line from the top-level section down to this line."""
    lineage: tuple[Mapping[str, str], ...]
    """`MatchRule` dicts that select this line, in the shape `PlanRequest` uses."""


class FutureReportData(BaseModel):
    """How the negations of a remediation resolve against the running config."""

    unresolved_negations: tuple[NegationFact, ...]
    """Negations whose positive form matches nothing on the device."""
    idempotency_replacements: tuple[NegationFact, ...]
    """Negations that stay in the config because they replace a tracked line."""


class RemediationBundle(BaseModel):
    """The configs and facts that one change on one device needs."""

    platform_name: str
    remediation_dump: DumpJson
    rollback_dump: DumpJson
    future_dump: DumpJson
    future_report: FutureReportData
