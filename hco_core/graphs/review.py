"""The review graph: collect, remediate, gate, park for a human, record the decision.

No node calls a model. The convergence gate blocks a remediation with an
unresolved negation before any assessment node runs. Every config in the state
is `Dump` JSON. Each node that changes the run state writes it through
`RunRepository` immediately before it returns.
"""

import operator
from collections.abc import Callable
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
from pydantic import JsonValue, ValidationError

from hco_core.adapters.errors import AdapterError
from hco_core.domain.approval import Decision, RunState
from hco_core.domain.base import BaseModel
from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.domain.notify import Notification
from hco_core.domain.remediation import DumpJson
from hco_core.domain.risk import RiskTier
from hco_core.domain.secrets import SecretPurpose
from hco_core.engine.errors import EngineError
from hco_core.engine.fingerprints import change_fingerprint
from hco_core.engine.remediation import build_bundle, parse
from hco_core.engine.views import config_derived
from hco_core.policy.tiers import APPROVALS_BY_TIER
from hco_core.ports import ConnectionDriver, SecretsProvider, SourceOfTruth
from hco_core.services.approval import ApprovalService
from hco_core.services.audit import Actor
from hco_core.services.ledger import NotificationLedger
from hco_core.services.runs import RunRepository, RunUpdate, Snapshot

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

DURABILITY: Final[Durability] = "sync"
"""The run parks for a human, so each step commits its checkpoint before the next."""
NO_ASSESSMENT: Final = "no risk assessment"
GRAPH_ACTOR: Final = Actor("hco-core", "system", "api")


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


type AssessmentHook = Callable[[StateGraph[ReviewState]], tuple[str, str]]
"""Add assessment nodes to the graph and return the entry and the exit node names."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewDeps:
    """The adapters and services that the nodes call."""

    sot: SourceOfTruth
    secrets: SecretsProvider
    connection: ConnectionDriver
    runs: RunRepository
    approvals: ApprovalService
    ledger: NotificationLedger
    channels: tuple[str, ...] = ("log",)
    approval_ttl: timedelta = timedelta(hours=72)
    actor: Actor = GRAPH_ACTOR


class IntendedConfigMissingError(LookupError):
    """The source of truth has no intended config for the device."""


class MissingStateError(LookupError):
    """The input or an earlier node did not set a key that a node reads."""


class _Resume(BaseModel):
    decision: Decision
    actor: str
    comment: str = ""


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
        bundle = build_bundle(platform_name, running_dump, intended_dump)
        derived = config_derived(bundle, running_dump, device)
        fingerprint = change_fingerprint(bundle.remediation_dump)
    except EngineError as error:
        return {"errors": [f"build_remediation: {error}"]}
    future_report = bundle.future_report.model_dump(mode="json")
    await deps.runs.update_run(
        _need(state.get("run_id"), "run_id"),
        RunUpdate(
            state=RunState.REMEDIATING,
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


async def evaluate_policy(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """Set the final tier and the approvals that it needs.

    With no assessment tier, the tier is `HIGH` and `unknowns` names it. The
    final tier is never below the floor.
    """
    unknowns: list[str] = []
    try:
        floor = _tier(state.get("risk_tier_floor"))
        assessed = _tier(state.get("risk_tier_final"))
    except KeyError as error:
        return {"errors": [f"evaluate_policy: unknown risk tier {error}"]}
    if assessed is None:
        assessed = RiskTier.HIGH
        unknowns.append(NO_ASSESSMENT)
    final = max(assessed, floor or RiskTier.LOW)
    expires_at = datetime.now(UTC) + deps.approval_ttl
    await deps.runs.update_run(
        _need(state.get("run_id"), "run_id"),
        RunUpdate(
            state=RunState.EVALUATING_POLICY,
            risk_tier_floor=floor,
            risk_tier_final=final,
        ),
        deps.actor,
    )
    return {
        "risk_tier_final": final.name,
        "policy": {
            "approvals_required": APPROVALS_BY_TIER[final],
            "approval_expires_at": expires_at.isoformat(),
        },
        "unknowns": unknowns,
        "run_state": RunState.EVALUATING_POLICY,
    }


def _tier(name: str | None) -> RiskTier | None:
    return None if name is None else RiskTier[name]


async def notify_pending(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """Tell each channel that the run waits for approval, one time."""
    run_id = _need(state.get("run_id"), "run_id")
    tier = _need(state.get("risk_tier_final"), "risk_tier_final")
    policy = _need(state.get("policy"), "policy")
    message = Notification(
        title=f"Approval needed for run {run_id}",
        body=(
            f"Run {run_id} on device {_device(state).name} waits for approval. "
            f"Risk tier: {tier}. "
            f"Approvals needed: {policy['approvals_required']}."
        ),
        severity="warning",
        run_id=run_id,
    )
    for channel in deps.channels:
        await deps.ledger.deliver(run_id, channel, "pending", message)
    return {}


async def request_approval(state: ReviewState, deps: ReviewDeps) -> ReviewState:
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
    )
    decision = interrupt(
        {
            "kind": "approval",
            "request_id": request.id,
            "tier": tier,
            "summary": _summary(state, tier),
        }
    )
    return {"approval": decision, "run_state": RunState.AWAITING_APPROVAL}


def _summary(state: ReviewState, tier: str) -> str:
    summary = state.get("risk", {}).get("summary")
    if isinstance(summary, str) and summary:
        return summary
    return f"No risk assessment ran. The tier is {tier} by default."


async def record_decision(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """Set `APPROVED` or `REJECTED` from the resume payload.

    A payload that is not a valid decision rejects the run and records why.
    """
    run_id = _need(state.get("run_id"), "run_id")
    try:
        resume = _Resume.model_validate(state.get("approval"))
    except ValidationError as error:
        reason = f"record_decision: the resume payload is not valid: {error}"
        await deps.runs.update_run(
            run_id,
            RunUpdate(state=RunState.REJECTED, error={"errors": [reason]}),
            deps.actor,
        )
        return {"errors": [reason], "run_state": RunState.REJECTED}
    final = (
        RunState.APPROVED if resume.decision is Decision.APPROVE else RunState.REJECTED
    )
    await deps.runs.update_run(run_id, RunUpdate(state=final), deps.actor)
    return {"approval": resume.model_dump(mode="json"), "run_state": final}


async def notify_decided(state: ReviewState, deps: ReviewDeps) -> ReviewState:
    """Tell each channel how the run ended, one time."""
    run_id = _need(state.get("run_id"), "run_id")
    outcome = _need(state.get("run_state"), "run_state").lower()
    actor = state.get("approval", {}).get("actor")
    by = f" by {actor}" if isinstance(actor, str) else ""
    message = Notification(
        title=f"Run {run_id} {outcome}",
        body=f"Run {run_id} on device {_device(state).name} was {outcome}{by}.",
        run_id=run_id,
    )
    for channel in deps.channels:
        await deps.ledger.deliver(run_id, channel, "decided", message)
    return {}


def _device(state: ReviewState) -> DeviceRecord:
    return DeviceRecord.model_validate(_need(state.get("device"), "device"))


def _need[T](value: T | None, key: str) -> T:
    if value is None:
        message = f"the review state has no {key!r}"
        raise MissingStateError(message)
    return value


def build_review_graph(
    deps: ReviewDeps, *, assessment: AssessmentHook | None = None
) -> StateGraph[ReviewState]:
    """Return the uncompiled review graph.

    The hook adds its nodes between the convergence gate and `evaluate_policy`.
    Compile the graph with a checkpointer, and start it with `start_run`.
    """
    graph = StateGraph(ReviewState)
    nodes = {
        "collect_running": collect_running,
        "build_remediation": build_remediation,
        "blocked": blocked,
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
    graph.add_edge(START, "collect_running")
    graph.add_conditional_edges(
        "collect_running",
        route_on_errors,
        {"blocked": "blocked", "pass": "build_remediation"},
    )
    graph.add_conditional_edges(
        "build_remediation",
        route_after_convergence,
        {"blocked": "blocked", "pass": entry},
    )
    if exit_ is not None:
        graph.add_edge(exit_, "evaluate_policy")
    graph.add_conditional_edges(
        "evaluate_policy",
        route_on_errors,
        {"blocked": "blocked", "pass": "notify_pending"},
    )
    graph.add_edge("notify_pending", "request_approval")
    graph.add_edge("request_approval", "record_decision")
    graph.add_edge("record_decision", "notify_decided")
    graph.add_edge("notify_decided", END)
    graph.add_edge("blocked", END)
    return graph


async def start_run(
    graph: CompiledStateGraph[ReviewState], state: ReviewState, thread_id: str
) -> None:
    """Run the graph on the thread until it parks or ends."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(state, config, durability=DURABILITY)  # pyright: ignore[reportUnknownMemberType]
