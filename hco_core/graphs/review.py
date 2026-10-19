"""The review graph: collect, remediate, and gate on unresolved negations.

No node calls a model. The convergence gate blocks a remediation with an
unresolved negation before any later node runs. Every config in the state is
`Dump` JSON. Each node that changes the run state writes it through
`RunRepository` immediately before it returns.
"""

import operator
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, TypedDict

from langgraph.graph import (  # pyright: ignore[reportMissingTypeStubs]
    END,
    START,
    StateGraph,
)
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    CompiledStateGraph,
)
from langgraph.types import Durability
from pydantic import JsonValue

from hco_core.adapters.errors import AdapterError
from hco_core.domain.approval import RunState
from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.domain.remediation import DumpJson
from hco_core.domain.secrets import SecretPurpose
from hco_core.engine.errors import EngineError
from hco_core.engine.fingerprints import change_fingerprint
from hco_core.engine.remediation import build_bundle, parse, render_lines
from hco_core.engine.views import config_derived
from hco_core.ports import ConnectionDriver, SecretsProvider, SourceOfTruth
from hco_core.services.approval import ApprovalService
from hco_core.services.audit import Actor
from hco_core.services.ledger import NotificationLedger
from hco_core.services.runs import RunRepository, RunUpdate, Snapshot

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

DURABILITY: Final[Durability] = "sync"
"""The run parks for a human, so each step commits its checkpoint before the next."""
GRAPH_ACTOR: Final = Actor("hco-core", "system", "api")

type PluginFactory = Callable[[DeviceRecord], Iterable[Callable[..., None]]]
"""Return the remediation plugins of the device. Each takes the remediation tree."""


def no_plugins(device: DeviceRecord) -> tuple[()]:
    """Return no remediation plugins."""
    del device
    return ()


def merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Return the left mapping updated with the right one."""
    return {**left, **right}


class ApprovalPolicy(TypedDict):
    """What the approval request of the run needs."""

    approvals_required: int
    approval_expires_at: str


class ReviewState(TypedDict, total=False):
    """The state of one run, as docs/spec/07 defines it.

    `evidence` holds JSON from parallel assessment nodes, keyed by source. Each
    source has its own shape, so the value type is `Any`. `errors` and
    `unknowns` add up across nodes.
    """

    run_id: str
    change_set_id: str
    tenant_id: str | None
    device_id: str
    """The id of the `device` row."""
    sot_device_id: str
    """The id of the device in the source of truth."""
    device: dict[str, JsonValue]
    platform_name: str
    requested_by: str
    plan_source: Literal["deterministic", "ai", "manual"]
    run_state: str
    running_dump: DumpJson
    intended_dump: DumpJson
    remediation_dump: DumpJson
    rollback_dump: DumpJson
    future_dump: DumpJson
    future_report: dict[str, JsonValue]
    change_fingerprint: str
    evidence: Annotated[dict[str, Any], merge_dicts]
    risk_tier_floor: str
    risk: dict[str, JsonValue]
    risk_tier_final: str
    policy: ApprovalPolicy
    approval: dict[str, JsonValue]
    errors: Annotated[list[str], operator.add]
    unknowns: Annotated[list[str], operator.add]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewDeps:
    """The adapters and services that the nodes call."""

    sot: SourceOfTruth
    secrets: SecretsProvider
    connection: ConnectionDriver
    runs: RunRepository
    approvals: ApprovalService
    ledger: NotificationLedger
    plugins: PluginFactory = no_plugins
    tag_rules: tuple[Mapping[str, object], ...] = ()
    channels: tuple[str, ...] = ("log",)
    approval_ttl: timedelta = timedelta(hours=72)
    actor: Actor = GRAPH_ACTOR


class IntendedConfigMissingError(LookupError):
    """The source of truth has no intended config for the device."""


class MissingStateError(LookupError):
    """The input or an earlier node did not set a key that a node reads."""


@dataclass(frozen=True, slots=True)
class _Collected:
    device: DeviceRecord
    running_dump: DumpJson
    intended_dump: DumpJson
    running_snapshot_id: str
    intended_snapshot_id: str


async def collect_running(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """Read the device and its intended config, then collect the running config."""
    try:
        collected = await _collect(state, deps)
    except (AdapterError, EngineError, IntendedConfigMissingError, OSError) as error:
        return {"errors": [f"collect_running: {error}"]}
    await deps.runs.update_run(
        _need(state.get("run_id"), "run_id"),
        RunUpdate(
            state=RunState.COLLECTING,
            running_snapshot_id=collected.running_snapshot_id,
            intended_snapshot_id=collected.intended_snapshot_id,
        ),
        deps.actor,
    )
    return {
        "device": collected.device.model_dump(mode="json"),
        "platform_name": collected.device.platform_name,
        "running_dump": collected.running_dump,
        "intended_dump": collected.intended_dump,
        "run_state": RunState.COLLECTING,
    }


async def _collect(state: ReviewState, deps: ReviewDeps) -> _Collected:
    device = await deps.sot.get_device(
        DeviceID(_need(state.get("sot_device_id"), "sot_device_id"))
    )
    device_id = _need(state.get("device_id"), "device_id")
    intended = await deps.sot.intended_config(device.id)
    if intended is None:
        message = f"no intended config for device {device.id!r}"
        raise IntendedConfigMissingError(message)
    ref = await deps.sot.secret_ref(device.id, SecretPurpose.DEVICE_LOGIN)
    credentials = await deps.secrets.resolve(ref)
    async with deps.connection.session(device, credentials) as session:
        running = await session.fetch_running()
    running_dump = parse(device.platform_name, running.body)
    intended_dump = parse(device.platform_name, intended.body)
    running_id = await deps.runs.add_snapshot(
        Snapshot(
            device_id=device_id,
            kind="running",
            body=running.body,
            dump=running_dump,
            format=running.format,
            source=running.source,
            device_fingerprint=running.fingerprint,
        )
    )
    intended_id = await deps.runs.add_snapshot(
        Snapshot(
            device_id=device_id,
            kind="intended",
            body=intended.body,
            dump=intended_dump,
            format=intended.format,
            source="sot",
        )
    )
    return _Collected(device, running_dump, intended_dump, running_id, intended_id)


def route_on_errors(state: ReviewState) -> Literal["blocked", "pass"]:
    """Send a run with an error to `blocked`."""
    return "blocked" if state.get("errors") else "pass"


async def build_remediation(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """Build the remediation, the rollback, the future, and the config facts."""
    running_dump = _need(state.get("running_dump"), "running_dump")
    intended_dump = _need(state.get("intended_dump"), "intended_dump")
    platform_name = _need(state.get("platform_name"), "platform_name")
    device = _device(state)
    try:
        bundle = build_bundle(
            platform_name,
            running_dump,
            intended_dump,
            plugins=tuple(deps.plugins(device)),
            tag_rules=deps.tag_rules,
        )
        future_text = "".join(
            f"{line}\n" for line in render_lines(platform_name, bundle.future_dump)
        )
        derived = config_derived(bundle, running_dump, device)
        fingerprint = change_fingerprint(bundle.remediation_dump)
    except EngineError as error:
        return {"errors": [f"build_remediation: {error}"]}
    future_report = bundle.future_report.model_dump(mode="json")
    future_id = await deps.runs.add_snapshot(
        Snapshot(
            device_id=_need(state.get("device_id"), "device_id"),
            kind="future",
            body=future_text,
            dump=bundle.future_dump,
            source="sot",
        )
    )
    await deps.runs.update_run(
        _need(state.get("run_id"), "run_id"),
        RunUpdate(
            state=RunState.REMEDIATING,
            future_snapshot_id=future_id,
            remediation_dump=bundle.remediation_dump,
            rollback_dump=bundle.rollback_dump,
            future_report=future_report,
            change_fingerprint=fingerprint,
        ),
        deps.actor,
    )
    return {
        "remediation_dump": bundle.remediation_dump,
        "rollback_dump": bundle.rollback_dump,
        "future_dump": bundle.future_dump,
        "future_report": future_report,
        "change_fingerprint": fingerprint,
        "plan_source": "deterministic",
        "evidence": {"config_derived": derived.model_dump(mode="json")},
        "run_state": RunState.REMEDIATING,
    }


def route_after_convergence(state: ReviewState) -> Literal["blocked", "pass"]:
    """Send a run with an error or an unresolved negation to `blocked`.

    No model is consulted. No later node can undo this gate.
    """
    report = state.get("future_report", {})
    if state.get("errors") or report.get("unresolved_negations"):
        return "blocked"
    return "pass"


async def blocked(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """End the run in `BLOCKED`, with each error recorded."""
    errors = list(state.get("errors", []))
    unresolved = state.get("future_report", {}).get("unresolved_negations")
    if isinstance(unresolved, list) and unresolved:
        errors.append(f"convergence gate: {len(unresolved)} unresolved negation(s)")
    await deps.runs.update_run(
        _need(state.get("run_id"), "run_id"),
        RunUpdate(state=RunState.BLOCKED, error={"errors": list[JsonValue](errors)}),
        deps.actor,
    )
    return {"run_state": RunState.BLOCKED}


def _device(state: ReviewState) -> DeviceRecord:
    return DeviceRecord.model_validate(_need(state.get("device"), "device"))


def _need[T](value: T | None, key: str) -> T:
    if value is None:
        message = f"the review state has no {key!r}"
        raise MissingStateError(message)
    return value


def add_collect_nodes(
    graph: StateGraph[ReviewState], deps: ReviewDeps, *, gate_passed: str
) -> None:
    """Add the nodes up to the convergence gate, and `blocked`.

    A run that passes the gate goes to `gate_passed`. A run with an error goes
    to `blocked`, which ends the graph.
    """
    nodes = {
        "collect_running": collect_running,
        "build_remediation": build_remediation,
        "blocked": blocked,
    }
    for name, node in nodes.items():
        graph.add_node(name, partial(node, deps=deps))  # pyright: ignore[reportUnknownMemberType]
    graph.add_edge(START, "collect_running")
    graph.add_conditional_edges(
        "collect_running",
        route_on_errors,
        {"blocked": "blocked", "pass": "build_remediation"},
    )
    graph.add_conditional_edges(
        "build_remediation",
        route_after_convergence,
        {"blocked": "blocked", "pass": gate_passed},
    )
    graph.add_edge("blocked", END)


def build_collect_graph(deps: ReviewDeps) -> StateGraph[ReviewState]:
    """Return the graph up to the convergence gate. A passed run ends there."""
    graph = StateGraph(ReviewState)
    add_collect_nodes(graph, deps, gate_passed=END)
    return graph


async def start_run(
    graph: CompiledStateGraph[ReviewState], state: ReviewState, thread_id: str
) -> None:
    """Run the graph on the thread until it parks or ends."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(state, config, durability=DURABILITY)  # pyright: ignore[reportUnknownMemberType]
