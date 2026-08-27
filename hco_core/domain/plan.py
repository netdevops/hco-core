"""Requesting and receiving an authored remediation plan."""

from collections.abc import Mapping
from typing import Any

from hco_core.domain.base import BaseModel


class PlanRequest(BaseModel):
    """Ask an analyser to author commands for a change a rule engine cannot express.

    The motivating case is an ACL resequence on Cisco IOS. The deterministic
    remediation removes the ACL and rebuilds it, and between those two commands
    an interface with `ip access-group` referencing a missing ACL denies all
    traffic. The correct procedure inserts a temporary low-sequence permit,
    resequences beneath it, then removes it -- add-then-remove scaffolding, whose
    net effect is nothing and which no diff engine can generate, because the
    *path* between two states matters and a diff has no concept of a path.
    """

    platform_name: str
    running_dump: Mapping[str, Any]
    intended_dump: Mapping[str, Any]
    lineage: tuple[Mapping[str, Any], ...] = ()
    """`MatchRule` dicts scoping the section in question."""
    instruction: str = ""
    """Prose from a network engineer describing how this change is done safely.

    The highest-leverage field here: it is where organisational knowledge becomes
    something an analyser can act on.
    """
    examples: tuple[Mapping[str, Any], ...] = ()
    retrieved: tuple[Mapping[str, Any], ...] = ()
    max_attempts: int = 3


class PlanResult(BaseModel):
    """An authored plan, and what the analyser knows about it."""

    commands: tuple[str, ...]
    reasoning: str = ""
    confidence: str = "medium"
    commands_requiring_review: tuple[str, ...] = ()
    scaffolding: tuple[str, ...] = ()
    """Commands the plan adds and then removes, in that order, whose net effect
    is nothing.

    Both constraints carry weight. A loose matcher would treat `no ip` as
    cancelling `ip route 0.0.0.0 0.0.0.0 198.51.100.66`, because in a tree model
    it does -- but on the device it is `% Incomplete command` and the route
    stays. Cancellation in a model is not cancellation on a router.
    """
    attempts: int = 1
    metadata: Mapping[str, Any] = {}
