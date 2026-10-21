"""The review graph: collect, remediate, and gate on unresolved negations.

No node calls a model. The convergence gate blocks a remediation with an
unresolved negation before any later node runs. Every config in the state is
`Dump` JSON. Each node that changes the run state writes it through
`RunRepository` immediately before it returns. The run state guards each
write, so a node that runs again writes nothing new.
"""

import asyncio
import operator
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
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

from hco_core.domain.approval import RunState
from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.domain.remediation import ConfigDerived, DumpJson, RemediationBundle
from hco_core.domain.secrets import SecretPurpose
from hco_core.engine.fingerprints import change_fingerprint
from hco_core.engine.remediation import build_bundle, parse, render_lines
from hco_core.engine.views import config_derived
from hco_core.ports import ConnectionDriver, SecretsProvider, SourceOfTruth
from hco_core.services.audit import Actor
from hco_core.services.runs import RunRepository, RunUpdate, RunView, Snapshot

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

DURABILITY: Final[Durability] = "sync"
"""The run parks for a human, so each step commits its checkpoint before the next."""
GRAPH_ACTOR: Final = Actor("hco-core", "system", "api")
BLOCKABLE_STATES: Final = frozenset(
    {RunState.DRAFT, RunState.COLLECTING, RunState.REMEDIATING}
)
"""The run states that `blocked` may move to `BLOCKED`. A run in another state stays."""

NAMED_NEGATIONS: Final = 5
"""The most unresolved negations that the gate error names."""
NEGATION_CHARACTERS: Final = 120
"""The longest negation text that the gate error shows."""

type PluginFactory = Callable[[DeviceRecord], Iterable[Callable[..., None]]]
"""Return the remediation plugins of the device. Each takes the remediation tree."""


def no_plugins(device: DeviceRecord) -> tuple[()]:
    """Return no remediation plugins."""
    del device
    return ()


def merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Return the left mapping updated with the right one."""
    return {**left, **right}


class ReviewState(TypedDict, total=False):
    """The state of one run, as docs/spec/07 defines it.

    The input holds `run_id`, and `change_set_id` and `requested_by` for the
    approval. `evidence` holds JSON from parallel nodes, keyed by source; each
    source has its own shape, so the value type is `Any`. `errors` maps a node
    name to its error, or to an empty text when the node passed, so a node that
    runs again replaces its own entry.
    """

    run_id: str
    change_set_id: str
    tenant_id: str | None
    device_id: str
    """The id of the `device` row, from the run."""
    sot_device_id: str
    """The id of the device in the source of truth, from the `device` row."""
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
    errors: Annotated[dict[str, str], merge_dicts]
    unknowns: Annotated[list[str], operator.add]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewDeps:
    """The adapters and services that the nodes call."""

    sot: SourceOfTruth
    secrets: SecretsProvider
    connection: ConnectionDriver
    runs: RunRepository
    plugins: PluginFactory = no_plugins
    tag_rules: tuple[Mapping[str, object], ...] = ()
    actor: Actor = GRAPH_ACTOR


class MissingStateError(LookupError):
    """The input or an earlier node did not set a key that a node reads."""


class CollectionError(ValueError):
    """The source of truth or the device gave something that the run cannot use."""


class RunStartedError(RuntimeError):
    """The run left `COLLECTING` before, so it does not start again."""

    def __init__(self, state: RunState) -> None:
        super().__init__(f"the run is {state}")
        self.state = state


@dataclass(frozen=True, slots=True)
class _Collected:
    view: RunView
    device: DeviceRecord
    running_dump: DumpJson
    intended_dump: DumpJson


async def collect_running(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """Read the device and its intended config, then collect the running config.

    A run past `COLLECTING` ends with no write. Any error ends the run in
    `BLOCKED`, because the adapters and the device are outside HCO. A missing
    `run_id` is a wiring error and raises `MissingStateError`.
    """
    run_id = _need(state.get("run_id"), "run_id")
    try:
        collected = await _collect(run_id, deps)
    except RunStartedError as started:
        return {"run_state": started.state.value}
    except Exception as error:  # ruff: ignore[blind-except]
        return {"errors": {"collect_running": f"collect_running: {error}"}}
    view = collected.view
    return {
        "device_id": view.device_id,
        "sot_device_id": view.sot_device_id,
        "device": collected.device.model_dump(mode="json"),
        "platform_name": view.platform_name,
        "running_dump": collected.running_dump,
        "intended_dump": collected.intended_dump,
        "run_state": RunState.COLLECTING.value,
        "errors": {"collect_running": ""},
    }


async def _collect(run_id: str, deps: ReviewDeps) -> _Collected:
    view = await deps.runs.get_run(run_id)
    if view.state not in {RunState.DRAFT, RunState.COLLECTING}:
        raise RunStartedError(view.state)
    device = await deps.sot.get_device(DeviceID(view.sot_device_id))
    if (device.id, device.platform_name) != (view.sot_device_id, view.platform_name):
        message = (
            f"the source of truth returned {device.id!r} on {device.platform_name} "
            f"for {view.sot_device_id!r} on {view.platform_name}"
        )
        raise CollectionError(message)
    intended = await deps.sot.intended_config(device.id)
    if intended is None:
        message = f"no intended config for device {device.id!r}"
        raise CollectionError(message)
    ref = await deps.sot.secret_ref(device.id, SecretPurpose.DEVICE_LOGIN)
    credentials = await deps.secrets.resolve(ref)
    async with deps.connection.session(device, credentials) as session:
        running = await session.fetch_running()
    for name, body in (("running", running.body), ("intended", intended.body)):
        if "\x00" in body:
            message = f"the {name} config holds a NUL character"
            raise CollectionError(message)
    platform_name = view.platform_name
    running_dump = await asyncio.to_thread(parse, platform_name, running.body)
    intended_dump = await asyncio.to_thread(parse, platform_name, intended.body)
    running_dump, intended_dump = await deps.runs.record_collection(
        run_id,
        Snapshot(
            device_id=view.device_id,
            kind="running",
            body=running.body,
            dump=running_dump,
            format=running.format,
            source=running.source,
            device_fingerprint=running.fingerprint,
        ),
        Snapshot(
            device_id=view.device_id,
            kind="intended",
            body=intended.body,
            dump=intended_dump,
            format=intended.format,
            source="sot",
        ),
        deps.actor,
    )
    return _Collected(view, device, running_dump, intended_dump)


def route_after_collection(state: ReviewState) -> Literal["blocked", "pass", "end"]:
    """Send an error to `blocked`, and end a run that started before."""
    if _errors(state):
        return "blocked"
    return "pass" if state.get("run_state") == RunState.COLLECTING else "end"


@dataclass(frozen=True, slots=True)
class _Remediated:
    bundle: RemediationBundle
    derived: ConfigDerived
    fingerprint: str
    future_text: str


def _remediate(
    platform_name: str,
    running_dump: DumpJson,
    intended_dump: DumpJson,
    device: DeviceRecord,
    deps: ReviewDeps,
) -> _Remediated:
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
    return _Remediated(
        bundle,
        config_derived(bundle, running_dump, device),
        change_fingerprint(bundle.remediation_dump),
        future_text,
    )


async def build_remediation(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """Build the remediation, the rollback, the future, and the config facts.

    The engine runs in a worker thread. Any error, from the engine or from a
    plugin, ends the run in `BLOCKED`.
    """
    run_id = _need(state.get("run_id"), "run_id")
    device_id = _need(state.get("device_id"), "device_id")
    running_dump = _need(state.get("running_dump"), "running_dump")
    intended_dump = _need(state.get("intended_dump"), "intended_dump")
    platform_name = _need(state.get("platform_name"), "platform_name")
    device = DeviceRecord.model_validate(_need(state.get("device"), "device"))
    try:
        result = await asyncio.to_thread(
            _remediate, platform_name, running_dump, intended_dump, device, deps
        )
        bundle = result.bundle
        future_report = bundle.future_report.model_dump(mode="json")
        await deps.runs.record_remediation(
            run_id,
            Snapshot(
                device_id=device_id,
                kind="future",
                body=result.future_text,
                dump=bundle.future_dump,
                source="sot",
            ),
            RunUpdate(
                remediation_dump=bundle.remediation_dump,
                rollback_dump=bundle.rollback_dump,
                future_report=future_report,
                change_fingerprint=result.fingerprint,
            ),
            deps.actor,
        )
    except Exception as error:  # ruff: ignore[blind-except]
        return {"errors": {"build_remediation": f"build_remediation: {error}"}}
    return {
        "remediation_dump": bundle.remediation_dump,
        "rollback_dump": bundle.rollback_dump,
        "future_dump": bundle.future_dump,
        "future_report": future_report,
        "change_fingerprint": result.fingerprint,
        "plan_source": "deterministic",
        "evidence": {"config_derived": result.derived.model_dump(mode="json")},
        "run_state": RunState.REMEDIATING.value,
        "errors": {"build_remediation": ""},
    }


def route_after_convergence(state: ReviewState) -> Literal["blocked", "pass"]:
    """Send a run with an error or an unresolved negation to `blocked`.

    No model is consulted. No later node can undo this gate.
    """
    report = state.get("future_report", {})
    if _errors(state) or report.get("unresolved_negations"):
        return "blocked"
    return "pass"


async def blocked(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """End the run in `BLOCKED`, with each error recorded.

    A run outside `BLOCKABLE_STATES` keeps its state and its error.
    """
    run_id = _need(state.get("run_id"), "run_id")
    errors: list[JsonValue] = list(_errors(state))
    unresolved = state.get("future_report", {}).get("unresolved_negations")
    if isinstance(unresolved, list) and unresolved:
        errors.append(gate_error(unresolved))
    written = await deps.runs.update_run(
        run_id,
        RunUpdate(state=RunState.BLOCKED, error={"errors": errors}),
        deps.actor,
        only_from=BLOCKABLE_STATES,
    )
    return {"run_state": RunState.BLOCKED.value} if written else {}


def gate_error(unresolved: list[JsonValue]) -> str:
    """Return the run error that names the unresolved negations.

    It names the first `NAMED_NEGATIONS` lines, each cut to
    `NEGATION_CHARACTERS`, and counts the rest.
    """
    named = ", ".join(
        repr(_negation_text(item)[:NEGATION_CHARACTERS])
        for item in unresolved[:NAMED_NEGATIONS]
    )
    rest = len(unresolved) - NAMED_NEGATIONS
    more = f" and {rest} more" if rest > 0 else ""
    return (
        f"convergence gate: {len(unresolved)} unresolved negation(s): {named}{more}. "
        "Each one removes a line that the running config does not have. "
        "Remove or correct these lines in the intended config."
    )


def _negation_text(item: JsonValue) -> str:
    text = item.get("text") if isinstance(item, dict) else None
    return text if isinstance(text, str) else str(item)


def _errors(state: ReviewState) -> list[str]:
    return [error for error in state.get("errors", {}).values() if error]


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
        route_after_collection,
        {"blocked": "blocked", "pass": "build_remediation", "end": END},
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
