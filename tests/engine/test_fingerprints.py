"""A change fingerprint identifies one change across many devices."""

from __future__ import annotations

import os
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys

import pytest
from hier_config import Platform

from hco_core.domain.remediation import DumpLineJson
from hco_core.engine.errors import InvalidConfigError, UnknownPlatformError
from hco_core.engine.fingerprints import change_fingerprint, driver_fingerprint
from hco_core.engine.remediation import build_bundle, parse

IOS = "CISCO_IOS"
TREE = "interface Gi1\n description a\n shutdown\ninterface Gi2\n mtu 9000\n"
REORDERED = "interface Gi2\n mtu 9000\ninterface Gi1\n shutdown\n description a\n"
SET_RULE_DRIVER = """
from hier_config.models import MatchRule, OrderingRule
from hier_config.platforms.driver_base import HConfigDriverRules
from hier_config.platforms.generic.driver import HConfigDriverGeneric
from hier_config.registry import register_driver

from hco_core.engine.fingerprints import driver_fingerprint


class SetRuleDriver(HConfigDriverGeneric):
    @staticmethod
    def _instantiate_rules() -> HConfigDriverRules:
        names = frozenset({"alpha", "beta", "gamma", "delta", "epsilon"})
        rule = OrderingRule(match_rules=(MatchRule(equals=names),), weight=1)
        return HConfigDriverRules(ordering=[rule])


register_driver("HCO_TEST_SET_RULE", SetRuleDriver)
print(driver_fingerprint("HCO_TEST_SET_RULE"))
"""


def test_line_order_does_not_change_the_fingerprint() -> None:
    """Two dumps of one tree share one sha256 hex fingerprint."""
    first, second = parse(IOS, TREE), parse(IOS, REORDERED)
    assert first != second
    assert change_fingerprint(first) == change_fingerprint(second)
    assert re.fullmatch(r"[0-9a-f]{64}", change_fingerprint(first))


@pytest.mark.parametrize(
    "other",
    [
        "interface Gi1\n description b\n",
        "interface Gi1\ninterface Gi2\n description a\n",
    ],
)
def test_a_different_change_alters_the_fingerprint(other: str) -> None:
    """Changed text, or the same line in another section, is another change."""
    first = parse(IOS, "interface Gi1\n description a\ninterface Gi2\n")
    assert change_fingerprint(first) != change_fingerprint(parse(IOS, other))


def test_malformed_dump_is_invalid_config() -> None:
    """A dump that fails the schema is an engine error."""
    with pytest.raises(InvalidConfigError):
        change_fingerprint({"lines": "x"})


def test_depth_zero_line_is_invalid_config() -> None:
    """A line above the top level is an engine error."""
    line = DumpLineJson(depth=0, text="a", tags=[], comments=[], new_in_config=False)
    with pytest.raises(InvalidConfigError):
        change_fingerprint({"lines": [line]})


def test_deepest_valid_dump_has_a_fingerprint() -> None:
    """A dump as deep as `hier_config` allows gets a fingerprint."""
    lines = [
        DumpLineJson(
            depth=depth, text=f"l{depth}", tags=[], comments=[], new_in_config=False
        )
        for depth in range(1, 1001)
    ]
    assert re.fullmatch(r"[0-9a-f]{64}", change_fingerprint({"lines": lines}))


def test_generated_comments_do_not_change_the_fingerprint() -> None:
    """One removed interface is one change, whatever it held."""
    small = build_bundle(IOS, parse(IOS, "interface Gi9\n mtu 9000\n"), parse(IOS, ""))
    large = build_bundle(
        IOS, parse(IOS, "interface Gi9\n mtu 9000\n shutdown\n"), parse(IOS, "")
    )
    assert small.remediation_dump != large.remediation_dump
    assert change_fingerprint(small.remediation_dump) == change_fingerprint(
        large.remediation_dump
    )


def test_driver_fingerprint_is_stable_for_one_platform() -> None:
    """Two calls and any spelling of one platform give one sha256 hex digest."""
    fingerprint = driver_fingerprint(IOS)

    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    assert driver_fingerprint(IOS) == fingerprint
    assert driver_fingerprint(IOS.lower()) == fingerprint


def test_driver_fingerprint_differs_between_platforms() -> None:
    """Every `Platform` member has its own digest, also with empty rules."""
    fingerprints = {driver_fingerprint(platform.name) for platform in Platform}

    assert len(fingerprints) == len(Platform)


def test_driver_fingerprint_rejects_an_unknown_platform() -> None:
    """A name with no driver raises the engine error."""
    with pytest.raises(UnknownPlatformError):
        driver_fingerprint("NO_SUCH_PLATFORM")


def test_driver_fingerprint_does_not_depend_on_set_iteration_order() -> None:
    """A `frozenset` rule gives one digest under every hash seed."""
    fingerprints = {
        subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            [sys.executable, "-c", SET_RULE_DRIVER],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("1", "2", "3", "4")
    }

    assert len(fingerprints) == 1
