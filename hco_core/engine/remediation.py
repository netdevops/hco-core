"""Build a remediation, a rollback, and a future config from Dump JSON.

Each function raises an `EngineError` subclass and never a `hier_config` error.
"""

from collections.abc import Iterable, Mapping

from hier_config import HConfig, HConfigChild, TagRule, WorkflowRemediation
from hier_config.models import Dump
from hier_config.workflows import RemediationTransform

from hco_core.domain.remediation import (
    DumpJson,
    FutureReportData,
    NegationFact,
    RemediationBundle,
)
from hco_core.engine.errors import engine_errors


def parse(platform_name: str, text: str) -> DumpJson:
    """Parse config text into Dump JSON."""
    with engine_errors():
        return _dump_json(HConfig.from_text(platform_name, text))


def render_lines(platform_name: str, dump: Mapping[str, object]) -> tuple[str, ...]:
    """Render Dump JSON as indented config lines."""
    with engine_errors():
        return _load(platform_name, dump).to_lines()


def build_bundle(  # ruff: ignore[too-many-arguments]
    platform_name: str,
    running_dump: Mapping[str, object],
    intended_dump: Mapping[str, object],
    *,
    plugins: Iterable[RemediationTransform] = (),
    tag_rules: Iterable[Mapping[str, object]] = (),
    intended_platform_name: str | None = None,
) -> RemediationBundle:
    """Return the remediation, rollback, future config, and future report.

    Pass `intended_platform_name` when the intended config uses another platform.
    """
    with engine_errors():
        running = _load(platform_name, running_dump)
        intended = _load(intended_platform_name or platform_name, intended_dump)
        rules = tuple(TagRule.model_validate(rule) for rule in tag_rules)
        workflow = WorkflowRemediation(running, intended, plugins=tuple(plugins))
        workflow.apply_remediation_tag_rules(rules)
        remediation = workflow.remediation_config
        future, report = running.future_with_report(remediation)
        return RemediationBundle(
            platform_name=platform_name,
            remediation_dump=_dump_json(remediation),
            rollback_dump=_dump_json(workflow.rollback_config),
            future_dump=_dump_json(future),
            future_report=FutureReportData(
                unresolved_negations=_facts(report.unresolved_negations),
                idempotency_replacements=_facts(report.idempotency_replacements),
            ),
        )


def _load(platform_name: str, dump: Mapping[str, object]) -> HConfig:
    return HConfig.from_dump(platform_name, Dump.model_validate(dump))


def _dump_json(config: HConfig) -> DumpJson:
    return {
        "lines": [
            {
                "depth": line.depth,
                "text": line.text,
                "tags": sorted(line.tags),
                "comments": sorted(line.comments),
                "new_in_config": line.new_in_config,
            }
            for line in config.dump().lines
        ]
    }


def _facts(children: Iterable[HConfigChild]) -> tuple[NegationFact, ...]:
    return tuple(
        NegationFact(
            text=child.text,
            path=tuple(child.path()),
            lineage=tuple({"equals": node.text} for node in child.lineage()),
        )
        for child in children
    )
