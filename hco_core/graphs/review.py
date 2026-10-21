"""The review graph: collect, remediate, gate, park for a human, record the decision.

No node calls a model. The convergence gate blocks a remediation with an
unresolved negation before any assessment node runs. Every config in the state is
`Dump` JSON. Each node that changes the run state writes it through
`RunRepository` immediately before it returns. The run state guards each
write, so a node that runs again writes nothing new.
"""

import asyncio
import operator
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from langgraph.types import Durability, interrupt
from pydantic import Field, JsonValue, ValidationError

from hco_core.domain.approval import ApprovalState, Decision, RunState
from hco_core.domain.base import BaseModel
from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.domain.notify import Notification
from hco_core.domain.remediation import ConfigDerived, DumpJson, RemediationBundle
from hco_core.domain.risk import RiskTier
from hco_core.domain.secrets import SecretPurpose
from hco_core.engine.fingerprints import change_fingerprint
from hco_core.engine.remediation import build_bundle, parse, render_lines
from hco_core.engine.views import config_derived
from hco_core.policy.tiers import APPROVALS_BY_TIER
from hco_core.ports import ConnectionDriver, SecretsProvider, SourceOfTruth
from hco_core.services.approval import ApprovalService
from hco_core.services.audit import Actor
from hco_core.services.ledger import NotificationLedger
from hco_core.services.runs import RunRepository, RunUpdate, RunView, Snapshot

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

DURABILITY: Final[Durability] = "sync"
"""The run parks for a human, so each step commits its checkpoint before the next."""
NO_ASSESSMENT: Final = "no risk assessment"
GRAPH_ACTOR: Final = Actor("hco-core", "system", "api")
BLOCKABLE_STATES: Final = frozenset(
    {
        RunState.DRAFT,
        RunState.COLLECTING,
        RunState.REMEDIATING,
        RunState.ASSESSING,
        RunState.EVALUATING_POLICY,
    }
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


def higher_tier(left: str | None, right: str | None) -> str | None:
    """Return the higher of two risk tier names, so that no node lowers a tier.

    An empty text or None counts as no tier, and the other side stays. Any
    other name that is not a tier wins, so that `evaluate_policy` blocks on it.
    """
    if not right:
        return left
    if not left or right not in RiskTier.__members__:
        return right
    if left not in RiskTier.__members__:
        return left
    return max(RiskTier[left], RiskTier[right]).name


class ApprovalPolicy(TypedDict):
    """What the approval request of the run needs."""

    approvals_required: int
    approval_expires_at: str


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
    risk_tier_floor: Annotated[str, higher_tier]
    risk: dict[str, JsonValue]
    risk_tier_final: Annotated[str, higher_tier]
    policy: ApprovalPolicy
    approval: dict[str, JsonValue]
    approval_request_id: str
    decided_by: list[str]
    """Each decision of the closed request, as `actor (decision)`."""
    decision_recorded: bool
    errors: Annotated[dict[str, str], merge_dicts]
    unknowns: Annotated[list[str], operator.add]


_POLICY_FROM: Final = frozenset(
    {RunState.REMEDIATING, RunState.ASSESSING, RunState.EVALUATING_POLICY}
)
_AWAIT_FROM: Final = frozenset({RunState.EVALUATING_POLICY, RunState.AWAITING_APPROVAL})


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


type AssessmentHook = Callable[[StateGraph[ReviewState]], tuple[str, str]]
"""Add assessment nodes to the graph and return the entry and the exit node names.

A hook node must catch its own errors and write each one into `errors` under
its own node name; `evaluate_policy` then ends the run in `BLOCKED`. An error
that a hook node raises leaves the graph, and the run stays in its state.
"""


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


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalDeps(ReviewDeps):
    """The dependencies of the full review graph: the collect ones and the approval ones."""

    approvals: ApprovalService
    ledger: NotificationLedger
    channels: tuple[str, ...] = ("log",)
    approval_ttl: timedelta = timedelta(hours=72)


class _Resume(BaseModel):
    decision: Decision
    actor: Annotated[str, Field(min_length=1)]
    comment: str = ""


_OUTCOMES: Final = {
    ApprovalState.APPROVED: (Decision.APPROVE, RunState.APPROVED),
    ApprovalState.REJECTED: (Decision.REJECT, RunState.REJECTED),
}


def route_on_errors(state: ReviewState) -> Literal["blocked", "pass"]:
    """Send a run with an error to `blocked`."""
    return "blocked" if _errors(state) else "pass"


async def evaluate_policy(state: ReviewState, deps: ApprovalDeps) -> ReviewState:
    """Set the final tier and the approvals that it needs.

    With no assessment tier, the tier is `HIGH` and `unknowns` names it. The
    final tier is never below the floor.
    """
    run_id = _need(state.get("run_id"), "run_id")
    if _errors(state):
        return {}
    unknowns: list[str] = []
    try:
        floor = _tier(state.get("risk_tier_floor"))
        assessed = _tier(state.get("risk_tier_final"))
    except KeyError as error:
        return {
            "errors": {"evaluate_policy": f"evaluate_policy: unknown risk tier {error}"}
        }
    if assessed is None:
        assessed = RiskTier.HIGH
        unknowns.append(NO_ASSESSMENT)
    final = max(assessed, floor or RiskTier.LOW)
    expires_at = datetime.now(UTC) + deps.approval_ttl
    written = await deps.runs.update_run(
        run_id,
        RunUpdate(
            state=RunState.EVALUATING_POLICY,
            risk_tier_floor=floor,
            risk_tier_final=final,
        ),
        deps.actor,
        only_from=_POLICY_FROM,
    )
    if not written:
        return await _left(run_id, deps, "evaluate_policy")
    return {
        "risk_tier_final": final.name,
        "policy": {
            "approvals_required": APPROVALS_BY_TIER[final],
            "approval_expires_at": expires_at.isoformat(),
        },
        "unknowns": unknowns,
        "run_state": RunState.EVALUATING_POLICY.value,
        "errors": {"evaluate_policy": ""},
    }


def _tier(name: str | None) -> RiskTier | None:
    return RiskTier[name] if name else None


async def _left(run_id: str, deps: ReviewDeps, node: str) -> ReviewState:
    current = (await deps.runs.get_run(run_id)).state
    return {
        "run_state": current.value,
        "errors": {node: f"{node}: the run left the review; it is {current}"},
    }


async def notify_pending(state: ReviewState, deps: ApprovalDeps) -> ReviewState:
    """Tell each channel that the run waits for approval, one time."""
    run_id = _need(state.get("run_id"), "run_id")
    tier = _need(state.get("risk_tier_final"), "risk_tier_final")
    policy = _need(state.get("policy"), "policy")
    message = Notification(
        title=f"Approval needed for run {run_id}",
        body=(
            f"Run {run_id} on device {_device_name(state)} waits for approval. "
            f"Risk tier: {tier}. "
            f"Approvals needed: {policy['approvals_required']}."
        ),
        severity="warning",
        run_id=run_id,
    )
    for channel in deps.channels:
        await deps.ledger.deliver(run_id, channel, "pending", message)
    return {}


async def request_approval(state: ReviewState, deps: ApprovalDeps) -> ReviewState:
    """Open the approval request, then park until a human decides.

    The node runs again from the top on resume. The calls above `interrupt()`
    are idempotent, and notifications go out in other nodes.
    """
    tier = _need(state.get("risk_tier_final"), "risk_tier_final")
    policy = _need(state.get("policy"), "policy")
    request = await deps.approvals.upsert_request(
        _need(state.get("change_set_id"), "change_set_id"),
        RiskTier[tier],
        policy["approvals_required"],
        _need(state.get("requested_by"), "requested_by"),
        datetime.fromisoformat(policy["approval_expires_at"]),
    )
    await deps.runs.update_run(
        _need(state.get("run_id"), "run_id"),
        RunUpdate(state=RunState.AWAITING_APPROVAL),
        deps.actor,
        only_from=_AWAIT_FROM,
    )
    decision = interrupt(
        {
            "kind": "approval",
            "request_id": request.id,
            "tier": tier,
            "summary": _summary(state, tier),
        }
    )
    return {
        "approval": decision,
        "approval_request_id": request.id,
        "run_state": RunState.AWAITING_APPROVAL.value,
    }


def _summary(state: ReviewState, tier: str) -> str:
    summary = state.get("risk", {}).get("summary")
    if isinstance(summary, str) and summary:
        return summary
    return f"No risk assessment ran. The tier is {tier} by default."


async def record_decision(state: ReviewState, deps: ApprovalDeps) -> ReviewState:
    """Set the run state from the closed approval request.

    The request in the database decides, never the resume payload. The run is
    `APPROVED` or `REJECTED` only when the request is closed that way and the
    payload names the same decision. In every other case the run ends in
    `BLOCKED` with the reason. A run that left `AWAITING_APPROVAL` keeps its
    state, and no message goes out.
    """
    run_id = _need(state.get("run_id"), "run_id")
    request_id = _need(state.get("approval_request_id"), "approval_request_id")
    request, decisions = await deps.approvals.get_request(request_id)
    final, reason = _outcome(request.state, state.get("approval"))
    written = await deps.runs.update_run(
        run_id,
        RunUpdate(state=final, error={"errors": [reason]} if reason else None),
        deps.actor,
        only_from={RunState.AWAITING_APPROVAL},
    )
    if not written:
        return {
            **await _left(run_id, deps, "record_decision"),
            "decision_recorded": False,
        }
    decided_by = [f"{row.actor} ({row.decision})" for row in decisions]
    return {
        "run_state": final.value,
        "decided_by": decided_by if final is not RunState.BLOCKED else [],
        "decision_recorded": True,
        "errors": {"record_decision": reason},
    }


def _outcome(request_state: ApprovalState, payload: object) -> tuple[RunState, str]:
    try:
        resume = _Resume.model_validate(payload)
    except ValidationError as error:
        return (
            RunState.BLOCKED,
            f"record_decision: the resume payload is not valid: {error}",
        )
    decision, final = _OUTCOMES.get(request_state, (None, RunState.BLOCKED))
    if decision is None:
        return (
            RunState.BLOCKED,
            f"record_decision: the approval request is {request_state}",
        )
    if resume.decision is not decision:
        return (
            RunState.BLOCKED,
            (
                f"record_decision: the approval request is {request_state}, "
                f"but the resume says {resume.decision}"
            ),
        )
    return final, ""


def route_after_decision(state: ReviewState) -> Literal["notify", "end"]:
    """Announce a recorded decision, and end a run that left the review."""
    return "notify" if state.get("decision_recorded") else "end"


async def notify_decided(state: ReviewState, deps: ApprovalDeps) -> ReviewState:
    """Tell each channel how the run ended, one time.

    The message names the decisions of the closed request, never the payload.
    """
    run_id = _need(state.get("run_id"), "run_id")
    outcome = _need(state.get("run_state"), "run_state")
    tier = _need(state.get("risk_tier_final"), "risk_tier_final")
    decided_by = state.get("decided_by", [])
    by = f" Decisions: {', '.join(decided_by)}." if decided_by else ""
    message = Notification(
        title=f"Run {run_id} is {outcome}",
        body=(
            f"Run {run_id} on device {_device_name(state)} is {outcome}. "
            f"Risk tier: {tier}.{by}"
        ),
        run_id=run_id,
    )
    for channel in deps.channels:
        await deps.ledger.deliver(run_id, channel, "decided", message)
    return {}


def _device_name(state: ReviewState) -> str:
    return DeviceRecord.model_validate(_need(state.get("device"), "device")).name


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


def build_review_graph(
    deps: ApprovalDeps, *, assessment: AssessmentHook | None = None
) -> StateGraph[ReviewState]:
    """Return the uncompiled review graph.

    The hook adds its nodes between the convergence gate and `evaluate_policy`.
    Compile the graph with a checkpointer, and start it with `start_run`.
    """
    graph = StateGraph(ReviewState)
    nodes = {
        "evaluate_policy": evaluate_policy,
        "notify_pending": notify_pending,
        "request_approval": request_approval,
        "record_decision": record_decision,
        "notify_decided": notify_decided,
    }
    for name, node in nodes.items():
        graph.add_node(name, partial(node, deps=deps))  # pyright: ignore[reportUnknownMemberType]
    entry, exit_ = (
        ("evaluate_policy", None) if assessment is None else assessment(graph)
    )
    add_collect_nodes(graph, deps, gate_passed=entry)
    if exit_ is not None:
        graph.add_edge(exit_, "evaluate_policy")
    graph.add_conditional_edges(
        "evaluate_policy",
        route_on_errors,
        {"blocked": "blocked", "pass": "notify_pending"},
    )
    graph.add_edge("notify_pending", "request_approval")
    graph.add_edge("request_approval", "record_decision")
    graph.add_conditional_edges(
        "record_decision",
        route_after_decision,
        {"notify": "notify_decided", "end": END},
    )
    graph.add_edge("notify_decided", END)
    return graph


async def start_run(
    graph: CompiledStateGraph[ReviewState], state: ReviewState, thread_id: str
) -> None:
    """Run the graph on the thread until it parks or ends."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(state, config, durability=DURABILITY)  # pyright: ignore[reportUnknownMemberType]
