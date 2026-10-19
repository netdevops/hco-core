"""The review graph collects, remediates, gates, parks, and records (AC-4, AC-10)."""

import ast
import asyncio
import inspect
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    CompiledStateGraph,
)
from langgraph.types import Command, StateSnapshot
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.domain.approval import ApprovalDecision, ApprovalRequest
from hco_core.domain.notify import DeliveryHandle, Notification
from hco_core.engine.errors import DriverMismatchError
from hco_core.graphs import review
from hco_core.graphs.review import (
    ReviewDeps,
    ReviewState,
    build_review_graph,
    merge_dicts,
    start_run,
)
from hco_core.ports import NotificationChannel
from hco_core.services.approval import ApprovalService
from hco_core.services.audit import Actor
from hco_core.services.ledger import NotificationLedger
from hco_core.services.runs import RunRepository
from tests.conftest import REQUESTER

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

RUNNING = "hostname r1\ninterface GigabitEthernet1\n ip address 10.0.0.1 255.0.0.0\n"
INTENDED = RUNNING + "ntp server 192.0.2.10\n"
UNRESOLVED = RUNNING + "no ip http server\n"
ENVIRON = {"HCO_SECRET_R1_USERNAME": "admin", "HCO_SECRET_R1_PASSWORD": "x"}
INVENTORY = """devices:
  - id: r1
    name: edge-1
    platform_name: CISCO_IOS
    mgmt_address: 10.0.0.1
    intended_config: intended.txt
  - id: r2
    name: edge-2
    platform_name: CISCO_IOS
    mgmt_address: 10.0.0.2
"""
APPROVE = {"decision": "approve", "actor": "bob", "comment": "ok"}
type Graph = CompiledStateGraph[ReviewState]
type Hook = Callable[[StateGraph[ReviewState]], tuple[str, str]]


class RecordingChannel(NotificationChannel):
    """A channel that keeps every message it sends."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    @override
    async def send(self, message: Notification) -> DeliveryHandle:
        """Keep the message."""
        self.sent.append(message)
        return DeliveryHandle(channel="test", external_id=str(len(self.sent)))

    @override
    async def update(self, handle: DeliveryHandle, message: Notification) -> None:
        """Keep the new text."""
        self.sent.append(message)


@dataclass
class World:
    """The adapters, services, and the run of one test."""

    deps: ReviewDeps
    channel: RecordingChannel
    root: Path
    engine: Engine
    state: ReviewState


async def no_resume(request: ApprovalRequest, decision: ApprovalDecision) -> None:
    """Do nothing. The tests resume the graph themselves."""
    del request, decision
    await asyncio.sleep(0)


@pytest.fixture
def world(
    runner: asyncio.Runner,
    sessions: async_sessionmaker[AsyncSession],
    engine: Engine,
    device_id: str,
    tmp_path: Path,
) -> World:
    """Give an inventory, transcripts, the services, and one run in `DRAFT`."""
    (tmp_path / "inventory.yaml").write_text(INVENTORY, encoding="utf8")
    (tmp_path / "intended.txt").write_text(INTENDED, encoding="utf8")
    (tmp_path / "transcripts" / "r1").mkdir(parents=True)
    (tmp_path / "transcripts" / "r1" / "running.txt").write_text(RUNNING, "utf8")
    channel = RecordingChannel()
    runs = RunRepository(sessions)
    deps = ReviewDeps(
        sot=YamlSourceOfTruth(tmp_path / "inventory.yaml"),
        secrets=EnvSecretsProvider(ENVIRON),
        connection=MockConnectionDriver(tmp_path / "transcripts", token_key=b"k" * 32),
        runs=runs,
        approvals=ApprovalService(sessions, resume=no_resume),
        ledger=NotificationLedger(sessions, {"test": channel}),
        channels=("test",),
    )
    requester = Actor(REQUESTER, "user", "cli")
    change_set_id = runner.run(runs.create_change_set("change", requester))
    run_id = runner.run(
        runs.create_run(change_set_id, device_id, "thread-1", requester)
    )
    state: ReviewState = {
        "run_id": run_id,
        "change_set_id": change_set_id,
        "device_id": device_id,
        "sot_device_id": "r1",
        "requested_by": REQUESTER,
    }
    return World(deps, channel, tmp_path, engine, state)


def compiled(world: World, hook: Hook | None = None) -> Graph:
    """Compile the graph on a memory checkpointer."""
    graph = build_review_graph(world.deps, assessment=hook)
    return graph.compile(checkpointer=InMemorySaver())  # pyright: ignore[reportUnknownMemberType]


def start(runner: asyncio.Runner, world: World, hook: Hook | None = None) -> Graph:
    """Run the graph until it parks or ends, and return it."""
    graph = compiled(world, hook)
    runner.run(start_run(graph, world.state, "thread-1"))
    return graph


def snapshot(runner: asyncio.Runner, graph: Graph) -> StateSnapshot:
    """Return the checkpoint of the test thread."""
    return runner.run(graph.aget_state({"configurable": {"thread_id": "thread-1"}}))


def resume(runner: asyncio.Runner, graph: Graph, payload: object) -> None:
    """Resume the parked graph with the payload."""
    config: RunnableConfig = {"configurable": {"thread_id": "thread-1"}}
    runner.run(graph.ainvoke(Command(resume=payload), config, durability="sync"))  # pyright: ignore[reportUnknownMemberType]


def scalar_rows(engine: Engine, sql: str) -> list[tuple[object, ...]]:
    """Return the rows of a query."""
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql))]


def run_row(world: World) -> tuple[object, ...]:
    """Return the state, the final tier, and the error of the run."""
    (row,) = scalar_rows(
        world.engine,
        "SELECT state, risk_tier_final, error FROM hco.remediation_run",
    )
    return row


def transitions(world: World) -> list[object]:
    """Return the states that the audit trail records for the run, in order."""
    rows = scalar_rows(
        world.engine,
        "SELECT after->>'state' FROM hco.audit_event "
        "WHERE object_type = 'remediation_run' ORDER BY occurred_at, id",
    )
    return [state for (state,) in rows]


def test_merge_dicts_keeps_the_keys_of_both_sides() -> None:
    """A later key wins, and every other key stays."""
    assert merge_dicts({"a": 1, "b": 1}, {"b": 2, "c": 3}) == {"a": 1, "b": 2, "c": 3}


def test_with_no_assessment_the_run_parks_at_high(
    runner: asyncio.Runner, world: World
) -> None:
    """No hook gives `HIGH`, one unknown, one request, and one pending message."""
    graph = start(runner, world)

    parked = snapshot(runner, graph)
    assert parked.next == ("request_approval",)
    (pause,) = parked.interrupts
    ((request_id, required_count, requester),) = scalar_rows(
        world.engine,
        "SELECT id, required_count, requester FROM hco.approval_request",
    )
    assert pause.value == {
        "kind": "approval",
        "request_id": request_id,
        "tier": "HIGH",
        "summary": "No risk assessment ran. The tier is HIGH by default.",
    }
    assert (required_count, requester) == (2, REQUESTER)
    assert parked.values["risk_tier_final"] == "HIGH"
    assert parked.values["unknowns"] == ["no risk assessment"]
    derived = parked.values["evidence"]["config_derived"]
    assert derived["top_level_sections"] == ["ntp server 192.0.2.10"]
    assert run_row(world) == ("AWAITING_APPROVAL", "HIGH", None)
    assert transitions(world) == [
        "DRAFT",
        "COLLECTING",
        "REMEDIATING",
        "EVALUATING_POLICY",
        "AWAITING_APPROVAL",
    ]
    ledger = scalar_rows(
        world.engine, "SELECT purpose, state FROM hco.notification_delivery"
    )
    assert ledger == [("pending", "sent")]
    (message,) = world.channel.sent
    assert message.run_id == world.state.get("run_id")
    assert "edge-1" in message.body
    assert "ntp" not in message.title + message.body


def test_the_snapshots_hold_the_collected_configs(
    runner: asyncio.Runner, world: World
) -> None:
    """The run points at the running and the intended snapshot."""
    start(runner, world)

    rows = scalar_rows(
        world.engine,
        "SELECT s.kind, s.source, s.body_inline FROM hco.config_snapshot s "
        "JOIN hco.remediation_run r ON s.id IN "
        "(r.running_snapshot_id, r.intended_snapshot_id) ORDER BY s.kind",
    )
    assert rows == [("intended", "sot", INTENDED), ("running", "device", RUNNING)]


@pytest.mark.parametrize(
    ("payload", "final"),
    [(APPROVE, "APPROVED"), ({**APPROVE, "decision": "reject"}, "REJECTED")],
)
def test_the_decision_ends_the_run(
    runner: asyncio.Runner, world: World, payload: dict[str, str], final: str
) -> None:
    """The resume payload sets `APPROVED` or `REJECTED`, and one message says so."""
    graph = start(runner, world)
    resume(runner, graph, payload)

    assert snapshot(runner, graph).next == ()
    assert run_row(world) == (final, "HIGH", None)
    assert transitions(world)[-2:] == ["AWAITING_APPROVAL", final]
    ledger = scalar_rows(
        world.engine,
        "SELECT purpose FROM hco.notification_delivery ORDER BY purpose",
    )
    assert ledger == [("decided",), ("pending",)]
    decided = world.channel.sent[-1]
    assert final.lower() in decided.body
    assert "bob" in decided.body


def test_a_malformed_decision_rejects(runner: asyncio.Runner, world: World) -> None:
    """A resume payload without a valid decision never approves."""
    graph = start(runner, world)
    resume(runner, graph, {"decision": "yes", "actor": "bob"})

    state, _, error = run_row(world)
    assert state == "REJECTED"
    assert error is not None
    assert "record_decision" in str(error)


def unresolved_negation(world: World, patch: pytest.MonkeyPatch) -> None:
    """Ask for a negation of a line that r1 does not have."""
    del patch
    (world.root / "intended.txt").write_text(UNRESOLVED, encoding="utf8")


def unknown_device(world: World, patch: pytest.MonkeyPatch) -> None:
    """Point the run at a device that the inventory does not hold."""
    del patch
    world.state["sot_device_id"] = "r9"


def no_intent(world: World, patch: pytest.MonkeyPatch) -> None:
    """Point the run at a device with no intended config."""
    del patch
    world.state["sot_device_id"] = "r2"


def unreachable(world: World, patch: pytest.MonkeyPatch) -> None:
    """Remove the transcripts of r1."""
    del patch
    (world.root / "transcripts" / "r1" / "running.txt").unlink()
    (world.root / "transcripts" / "r1").rmdir()


def driver_mismatch(world: World, patch: pytest.MonkeyPatch) -> None:
    """Make the engine raise as it does for two drivers."""
    del world

    def mismatch(*args: object, **kwargs: object) -> None:
        del args, kwargs
        message = "CISCO_IOS and CISCO_NXOS"
        raise DriverMismatchError(message)

    patch.setattr(review, "build_bundle", mismatch)


@pytest.mark.parametrize(
    ("arrange", "expected"),
    [
        (unresolved_negation, "unresolved negation"),
        (unknown_device, "no device 'r9'"),
        (no_intent, "no intended"),
        (unreachable, "no transcript"),
        (driver_mismatch, "CISCO_NXOS"),
    ],
    ids=["unresolved", "sot", "no-intent", "unreachable", "driver-mismatch"],
)
def test_a_failure_blocks_the_run_before_any_assessment(
    runner: asyncio.Runner,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Callable[[World, pytest.MonkeyPatch], object],
    expected: str,
) -> None:
    """The run ends `BLOCKED` with the error, and no assessment node runs (AC-4)."""
    calls: list[str] = []

    def hook(graph: StateGraph[ReviewState]) -> tuple[str, str]:
        def assess(state: ReviewState) -> ReviewState:
            calls.append(str(state.get("run_id")))
            return {}

        graph.add_node("assess", assess)  # pyright: ignore[reportUnknownMemberType]
        return "assess", "assess"

    arrange(world, monkeypatch)
    graph = start(runner, world, hook)

    assert snapshot(runner, graph).next == ()
    state, _, error = run_row(world)
    assert state == "BLOCKED"
    assert expected in str(error)
    assert calls == []
    assert transitions(world)[-1] == "BLOCKED"
    assert scalar_rows(world.engine, "SELECT id FROM hco.approval_request") == []
    assert world.channel.sent == []


def assessment(update: ReviewState) -> Hook:
    """Return a hook that adds one node, which writes the update."""

    def hook(graph: StateGraph[ReviewState]) -> tuple[str, str]:
        def assess(state: ReviewState) -> ReviewState:
            del state
            return update

        graph.add_node("assess", assess)  # pyright: ignore[reportUnknownMemberType]
        return "assess", "assess"

    return hook


def test_a_hook_node_runs_between_the_gate_and_the_policy(
    runner: asyncio.Runner, world: World
) -> None:
    """The tier and the summary of the hook reach the request."""
    hook = assessment(
        {
            "risk_tier_floor": "LOW",
            "risk_tier_final": "MEDIUM",
            "risk": {"summary": "Adds one NTP server."},
        }
    )
    graph = start(runner, world, hook)

    parked = snapshot(runner, graph)
    (pause,) = parked.interrupts
    value = cast("dict[str, object]", pause.value)
    assert (value["tier"], value["summary"]) == (
        "MEDIUM",
        "Adds one NTP server.",
    )
    assert parked.values.get("unknowns", []) == []
    assert scalar_rows(
        world.engine, "SELECT required_count FROM hco.approval_request"
    ) == [(1,)]
    assert run_row(world)[:2] == ("AWAITING_APPROVAL", "MEDIUM")


@pytest.mark.parametrize(
    ("update", "final", "unknowns"),
    [
        ({"risk_tier_floor": "CRITICAL", "risk_tier_final": "LOW"}, "CRITICAL", []),
        ({"risk_tier_floor": "CRITICAL"}, "CRITICAL", ["no risk assessment"]),
        ({}, "HIGH", ["no risk assessment"]),
    ],
    ids=["below-floor", "floor-only", "nothing"],
)
def test_the_policy_never_puts_the_tier_below_the_floor(
    runner: asyncio.Runner,
    world: World,
    update: ReviewState,
    final: str,
    unknowns: list[str],
) -> None:
    """The final tier is at least the floor, and a missing tier counts as `HIGH`."""
    graph = start(runner, world, assessment(update))

    parked = snapshot(runner, graph)
    assert parked.values["risk_tier_final"] == final
    assert parked.values.get("unknowns", []) == unknowns
    assert run_row(world)[:2] == ("AWAITING_APPROVAL", final)


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"errors": ["assess_risk: the model is unreachable"]}, "unreachable"),
        ({"risk_tier_final": "SEVERE"}, "SEVERE"),
    ],
    ids=["hook-error", "unknown-tier"],
)
def test_the_policy_blocks_on_an_error(
    runner: asyncio.Runner, world: World, update: ReviewState, expected: str
) -> None:
    """An error from the hook, or a tier with no name, ends in `BLOCKED`."""
    start(runner, world, assessment(update))

    state, _, error = run_row(world)
    assert state == "BLOCKED"
    assert expected in str(error)
    assert world.channel.sent == []


def test_request_approval_has_one_unconditional_interrupt() -> None:
    """One `interrupt()` call, outside every branch, after one `upsert_request`."""
    source = textwrap.dedent(inspect.getsource(review.request_approval))
    (function,) = ast.parse(source).body
    assert isinstance(function, ast.AsyncFunctionDef)

    def calls(name: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and name
            in {getattr(node.func, "id", None), getattr(node.func, "attr", None)}
        ]

    (pause,) = calls("interrupt")
    (upsert,) = calls("upsert_request")
    assert upsert.lineno < pause.lineno
    branches = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.With,
        ast.AsyncWith,
        ast.Match,
        ast.IfExp,
        ast.BoolOp,
        ast.comprehension,
    )
    enclosing = [
        node
        for node in ast.walk(function)
        if isinstance(node, branches)
        and any(child is pause for child in ast.walk(node))
    ]
    assert enclosing == []


def test_a_node_names_a_missing_key(runner: asyncio.Runner, world: World) -> None:
    """A node that reads a key that nothing set raises with the key name."""
    with pytest.raises(review.MissingStateError, match="'run_id'"):
        runner.run(review.notify_pending({}, world.deps))
