"""The remediation engine turns two Dump JSON configs into a change bundle."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hco_core.domain.remediation import DumpLineJson, NegationFact
from hco_core.engine.errors import (
    DriverMismatchError,
    InvalidConfigError,
    UnknownPlatformError,
)
from hco_core.engine.remediation import build_bundle, parse, render_lines

if TYPE_CHECKING:
    from hier_config import HConfig

CONFIGS = Path(__file__).resolve().parent.parent / "fixtures" / "configs"
PLATFORM_NAMES = ("CISCO_IOS", "CISCO_NXOS", "CISCO_XR", "ARISTA_EOS")


def fixture_text(platform_name: str, name: str) -> str:
    """Return one fixture config for a platform."""
    return (CONFIGS / platform_name.lower() / f"{name}.txt").read_text(encoding="utf8")


def test_parse_returns_dump_json() -> None:
    """A parsed config is plain JSON lines with depth, text, and tags."""
    dump = parse("CISCO_IOS", "interface Gi1\n description x\n")
    assert dump == {
        "lines": [
            {
                "depth": 1,
                "text": "interface Gi1",
                "tags": [],
                "comments": [],
                "new_in_config": False,
            },
            {
                "depth": 2,
                "text": "description x",
                "tags": [],
                "comments": [],
                "new_in_config": False,
            },
        ]
    }


@pytest.mark.parametrize("platform_name", PLATFORM_NAMES)
def test_render_lines_round_trips_the_dump(platform_name: str) -> None:
    """Rendered lines parse back to the same dump."""
    dump = parse(platform_name, fixture_text(platform_name, "running"))
    rendered = "\n".join(render_lines(platform_name, dump))
    assert parse(platform_name, rendered) == dump


@pytest.mark.parametrize("platform_name", PLATFORM_NAMES)
def test_build_bundle_on_each_platform(platform_name: str) -> None:
    """Each v1 platform yields a remediation, a rollback, and a clean report."""
    bundle = build_bundle(
        platform_name,
        parse(platform_name, fixture_text(platform_name, "running")),
        parse(platform_name, fixture_text(platform_name, "intended")),
    )
    assert bundle.platform_name == platform_name
    assert render_lines(platform_name, bundle.remediation_dump)
    assert render_lines(platform_name, bundle.rollback_dump)
    assert render_lines(platform_name, bundle.future_dump)
    assert bundle.future_report.unresolved_negations == ()
    assert bundle.future_report.idempotency_replacements == ()


def test_build_bundle_renders_remediation_rollback_and_future() -> None:
    """The bundle holds the three configs that the change needs."""
    bundle = build_bundle(
        "CISCO_IOS",
        parse("CISCO_IOS", "hostname old\n"),
        parse("CISCO_IOS", "hostname new\n"),
    )
    assert render_lines("CISCO_IOS", bundle.remediation_dump) == (
        "no hostname old",
        "hostname new",
    )
    assert render_lines("CISCO_IOS", bundle.rollback_dump) == (
        "no hostname new",
        "hostname old",
    )
    assert render_lines("CISCO_IOS", bundle.future_dump) == ("hostname new",)


def test_negation_of_an_absent_line_is_unresolved() -> None:
    """A negation that matches nothing on the device is a reported fact."""
    bundle = build_bundle(
        "CISCO_IOS",
        parse("CISCO_IOS", "hostname core-1\n"),
        parse("CISCO_IOS", "hostname core-1\nno ip http server\n"),
    )
    assert bundle.future_report.unresolved_negations == (
        NegationFact(
            text="no ip http server",
            path=("no ip http server",),
            lineage=({"equals": "no ip http server"},),
        ),
    )


def test_nested_negation_fact_carries_its_section() -> None:
    """A nested fact names the section that holds it."""
    bundle = build_bundle(
        "CISCO_IOS",
        parse("CISCO_IOS", "interface Gi1\n shutdown\n"),
        parse("CISCO_IOS", "interface Gi1\n no ip proxy-arp\n"),
    )
    (fact,) = bundle.future_report.unresolved_negations
    assert fact.path == ("interface Gi1", "no ip proxy-arp")
    assert fact.lineage == ({"equals": "interface Gi1"}, {"equals": "no ip proxy-arp"})


def test_idempotency_replacement_is_reported() -> None:
    """A negation that replaces a tracked counterpart is a reported fact."""
    bundle = build_bundle(
        "CISCO_IOS",
        parse("CISCO_IOS", "logging console informational\n"),
        parse("CISCO_IOS", "no logging console\n"),
    )
    assert [fact.text for fact in bundle.future_report.idempotency_replacements] == [
        "no logging console"
    ]
    assert bundle.future_report.unresolved_negations == ()


def test_tag_rules_tag_the_remediation_dump() -> None:
    """Tag rules apply before the remediation dump is read."""
    bundle = build_bundle(
        "CISCO_IOS",
        parse("CISCO_IOS", "hostname old\n"),
        parse("CISCO_IOS", "hostname new\n"),
        tag_rules=(
            {"match_rules": [{"startswith": "hostname"}], "apply_tags": ["safe"]},
        ),
    )
    tags = {line["text"]: line["tags"] for line in bundle.remediation_dump["lines"]}
    assert tags == {"no hostname old": [], "hostname new": ["safe"]}


def test_plugins_transform_the_remediation() -> None:
    """A remediation plugin changes the remediation before it is dumped."""

    def add_banner(remediation: HConfig) -> None:
        remediation.add_child("banner motd x")

    bundle = build_bundle(
        "CISCO_IOS",
        parse("CISCO_IOS", "hostname old\n"),
        parse("CISCO_IOS", "hostname new\n"),
        plugins=(add_banner,),
    )
    assert "banner motd x" in render_lines("CISCO_IOS", bundle.remediation_dump)


def test_different_drivers_raise_driver_mismatch() -> None:
    """Two configs from different platforms cannot be compared."""
    with pytest.raises(DriverMismatchError):
        build_bundle(
            "CISCO_IOS",
            parse("CISCO_IOS", "hostname a\n"),
            parse("CISCO_NXOS", "hostname b\n"),
            intended_platform_name="CISCO_NXOS",
        )


def test_unknown_platform_raises() -> None:
    """A platform with no registered driver is an engine error."""
    with pytest.raises(UnknownPlatformError):
        parse("NOT_A_PLATFORM", "hostname a\n")


def test_structured_text_is_invalid_config() -> None:
    """JSON given as CLI text is an engine error."""
    with pytest.raises(InvalidConfigError):
        parse("CISCO_IOS", '{"hostname": "a"}')


def test_malformed_dump_is_invalid_config() -> None:
    """A dump that fails the schema is an engine error."""
    with pytest.raises(InvalidConfigError):
        render_lines("CISCO_IOS", {"lines": [{"text": "a"}]})


def test_duplicate_dump_lines_are_invalid_config() -> None:
    """A dump with a duplicate section is an engine error."""
    line: DumpLineJson = {
        "depth": 1,
        "text": "a",
        "tags": [],
        "comments": [],
        "new_in_config": False,
    }
    with pytest.raises(InvalidConfigError):
        render_lines("CISCO_IOS", {"lines": [line, line]})


def test_malformed_tag_rule_is_invalid_config() -> None:
    """A tag rule that fails the schema is an engine error."""
    dump = parse("CISCO_IOS", "hostname a\n")
    with pytest.raises(InvalidConfigError):
        build_bundle("CISCO_IOS", dump, dump, tag_rules=({"apply_tags": 1},))
