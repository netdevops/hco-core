"""The remediation engine turns two Dump JSON configs into a change bundle."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hco_core.domain.remediation import (
    DumpLineJson,
    FutureReportData,
    NegationFact,
    RemediationBundle,
)
from hco_core.engine import errors
from hco_core.engine.remediation import (
    build_bundle,
    parse,
    render_lines,
    validated_dump,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from hier_config import HConfig

CONFIGS = Path(__file__).resolve().parent.parent / "fixtures" / "configs"
PLATFORM_NAMES = ("CISCO_IOS", "CISCO_NXOS", "CISCO_XR", "ARISTA_EOS")
IOS = "CISCO_IOS"
OLD = parse(IOS, "hostname old\n")
NEW = parse(IOS, "hostname new\n")


def fixture_text(platform_name: str, name: str) -> str:
    """Return one fixture config for a platform."""
    return (CONFIGS / platform_name.lower() / f"{name}.txt").read_text(encoding="utf8")


def line(depth: int, text: str) -> DumpLineJson:
    """Return one untagged Dump JSON line."""
    return DumpLineJson(
        depth=depth, text=text, tags=[], comments=[], new_in_config=False
    )


def ios(running: str, intended: str) -> RemediationBundle:
    """Return the bundle of two IOS config texts."""
    return build_bundle(IOS, parse(IOS, running), parse(IOS, intended))


def test_parse_returns_dump_json() -> None:
    """A parsed config is plain JSON lines with depth, text, and tags."""
    dump = parse(IOS, "interface Gi1\n description x\n")
    assert dump == {"lines": [line(1, "interface Gi1"), line(2, "description x")]}


@pytest.mark.parametrize("platform_name", PLATFORM_NAMES)
def test_bundle_round_trips_on_each_platform(platform_name: str) -> None:
    """Each v1 platform round-trips its dump and yields a clean bundle."""
    running = parse(platform_name, fixture_text(platform_name, "running"))
    intended = parse(platform_name, fixture_text(platform_name, "intended"))
    assert (
        parse(platform_name, "\n".join(render_lines(platform_name, running))) == running
    )
    bundle = build_bundle(platform_name, running, intended)
    assert bundle.platform_name == platform_name
    assert bundle.remediation_dump["lines"]
    assert bundle.future_report == FutureReportData(
        unresolved_negations=(), idempotency_replacements=()
    )


def test_build_bundle_renders_remediation_rollback_and_future() -> None:
    """The bundle holds the three configs that the change needs."""
    bundle = build_bundle(IOS, OLD, NEW)
    assert render_lines(IOS, bundle.remediation_dump) == (
        "no hostname old",
        "hostname new",
    )
    assert render_lines(IOS, bundle.rollback_dump) == (
        "no hostname new",
        "hostname old",
    )
    assert render_lines(IOS, bundle.future_dump) == ("hostname new",)


def test_negation_of_an_absent_line_is_unresolved() -> None:
    """A negation that matches nothing on the device is a reported fact."""
    report = ios("hostname core-1\n", "no ip http server\n").future_report
    text = "no ip http server"
    assert report.unresolved_negations == (
        NegationFact(text=text, path=(text,), lineage=({"equals": text},)),
    )


def test_nested_negation_fact_carries_its_section() -> None:
    """A nested fact names the section that holds it."""
    bundle = ios("interface Gi1\n shutdown\n", "interface Gi1\n no ip proxy-arp\n")
    (fact,) = bundle.future_report.unresolved_negations
    assert fact.path == ("interface Gi1", "no ip proxy-arp")
    assert fact.lineage == ({"equals": "interface Gi1"}, {"equals": "no ip proxy-arp"})


def test_idempotency_replacement_is_reported() -> None:
    """A negation that replaces a tracked counterpart is a reported fact."""
    report = ios(
        "logging console informational\n", "no logging console\n"
    ).future_report
    (fact,) = report.idempotency_replacements
    assert fact.text == "no logging console"
    assert report.unresolved_negations == ()


def test_tag_rules_tag_the_remediation_dump() -> None:
    """Tag rules apply before the remediation dump is read."""
    rule = {"match_rules": [{"startswith": "hostname"}], "apply_tags": ["safe"]}
    bundle = build_bundle(IOS, OLD, NEW, tag_rules=(rule,))
    tags = {row["text"]: row["tags"] for row in bundle.remediation_dump["lines"]}
    assert tags == {"no hostname old": [], "hostname new": ["safe"]}


def test_plugins_transform_the_remediation() -> None:
    """A remediation plugin changes the remediation before it is dumped."""

    def add_banner(remediation: HConfig) -> None:
        remediation.add_child("banner motd x")

    bundle = build_bundle(IOS, OLD, NEW, plugins=(add_banner,))
    assert "banner motd x" in render_lines(IOS, bundle.remediation_dump)


def mismatch() -> object:
    """Build a bundle from an IOS and an NX-OS config."""
    nxos = parse("CISCO_NXOS", "hostname b\n")
    return build_bundle(IOS, {"lines": []}, nxos, intended_platform_name="CISCO_NXOS")


ERROR_CASES: dict[str, tuple[Callable[[], object], type[errors.EngineError]]] = {
    "driver mismatch": (mismatch, errors.DriverMismatchError),
    "unknown platform": (
        lambda: parse("NOT_A_PLATFORM", ""),
        errors.UnknownPlatformError,
    ),
    "structured text": (lambda: parse(IOS, '{"a": 1}'), errors.InvalidConfigError),
    "malformed dump": (
        lambda: render_lines(IOS, {"lines": 1}),
        errors.InvalidConfigError,
    ),
    "duplicate lines": (
        lambda: render_lines(IOS, {"lines": [line(1, "a"), line(1, "a")]}),
        errors.InvalidConfigError,
    ),
    "text nested too deep": (
        lambda: parse(IOS, "".join(" " * i + f"l{i}\n" for i in range(1001))),
        errors.InvalidConfigError,
    ),
    "empty line text": (
        lambda: render_lines(IOS, {"lines": [line(1, "")]}),
        errors.InvalidConfigError,
    ),
    "depth zero line": (
        lambda: render_lines(IOS, {"lines": [line(0, "a")]}),
        errors.InvalidConfigError,
    ),
    "first line below the top level": (
        lambda: render_lines(IOS, {"lines": [line(3, "a")]}),
        errors.InvalidConfigError,
    ),
    "dump of the wrong shape": (
        lambda: validated_dump({"lines": "x"}),
        errors.InvalidConfigError,
    ),
    "depth skips a level": (
        lambda: render_lines(IOS, {"lines": [line(1, "a"), line(3, "b")]}),
        errors.InvalidConfigError,
    ),
    "malformed tag rule": (
        lambda: build_bundle(IOS, OLD, NEW, tag_rules=({"apply_tags": 1},)),
        errors.InvalidConfigError,
    ),
}


@pytest.mark.parametrize("case", ERROR_CASES)
def test_hier_config_errors_become_engine_errors(case: str) -> None:
    """No `hier_config` error leaves the engine."""
    call, error = ERROR_CASES[case]
    with pytest.raises(error):
        call()
