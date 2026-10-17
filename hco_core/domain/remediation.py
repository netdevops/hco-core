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
    lineage: tuple[Mapping[str, str], ...]
    """`MatchRule` dicts that select this line, in the shape `PlanRequest` uses."""


class FutureReportData(BaseModel):
    """How the negations of a remediation resolve against the running config."""

    unresolved_negations: tuple[NegationFact, ...]
    idempotency_replacements: tuple[NegationFact, ...]


class RemediationBundle(BaseModel):
    """The configs and facts that one change on one device needs."""

    platform_name: str
    remediation_dump: DumpJson
    rollback_dump: DumpJson
    future_dump: DumpJson
    future_report: FutureReportData


class DefaultRouteFact(BaseModel):
    """An IPv4 default route that a remediation adds or removes."""

    text: str
    """The route line, after the parent lines that the platform prefix names."""
    vrf: str | None
    """The VRF of the route, or None for the global table."""
    negated: bool
    """True if the remediation removes the route."""


class ConfigDerived(BaseModel):
    """Facts about what a remediation touches, with no judgement of risk.

    Names, texts, and default routes are sorted.
    """

    interfaces_touched: tuple[str, ...]
    bundles_touched: tuple[str, ...]
    vlans_touched: tuple[int, ...]
    nac_interfaces_touched: tuple[str, ...]
    unused_objects_before: tuple[str, ...]
    unused_objects_after: tuple[str, ...]
    unused_objects_created: tuple[str, ...]
    negation_count: int
    section_negations: tuple[str, ...]
    """Every negated top-level line."""
    command_count: int
    remediation_tags: tuple[str, ...]
    top_level_sections: tuple[str, ...]
    mgmt_interface: str | None
    """The first running interface by name that holds the management address, or None.

    Every holder counts for `mgmt_vrf` and `touches_mgmt_path`.
    """
    mgmt_vrf: str | None
    """The VRF of the holders, or None for the global table, no holder, or two VRFs."""
    default_routes_touched: tuple[DefaultRouteFact, ...]
    touches_mgmt_path: bool
    """True if the change touches a holder of the management address or a route in `mgmt_vrf`.

    A holder is any running interface that holds the management address. When
    `mgmt_vrf` is None, any default route and a default gateway change count.
    """
