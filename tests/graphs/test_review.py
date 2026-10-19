"""The review graph collects, remediates, and gates the run (AC-4)."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest
from hier_config import HConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    CompiledStateGraph,
)
from langgraph.types import StateSnapshot
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.errors import DeviceUnreachableError
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.engine.errors import DriverMismatchError
from hco_core.graphs import review
from hco_core.graphs.review import (
    ReviewDeps,
    ReviewState,
    add_collect_nodes,
    build_collect_graph,
    merge_dicts,
    start_run,
)
from hco_core.services.audit import Actor
from hco_core.services.runs import RunRepository
from tests.conftest import REQUESTER

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

RUNNING = "hostname r1\ninterface GigabitEthernet1\n ip address 10.0.0.1 255.0.0.0\n"
INTENDED = RUNNING + "ntp server 192.0.2.10\n"
UNRESOLVED = RUNNING + "no ip http server\n"
FUTURE = (
    "ntp server 192.0.2.10\nhostname r1\n"
    "interface GigabitEthernet1\n  ip address 10.0.0.1 255.0.0.0\n"
)
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
type Graph = CompiledStateGraph[ReviewState]


@dataclass
class World:
    """The adapters, services, and the run of one test."""

    deps: ReviewDeps
    root: Path
    engine: Engine
    state: ReviewState


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
    runs = RunRepository(sessions)
    deps = ReviewDeps(
        sot=YamlSourceOfTruth(tmp_path / "inventory.yaml"),
        secrets=EnvSecretsProvider(ENVIRON),
        connection=MockConnectionDriver(tmp_path / "transcripts", token_key=b"k" * 32),
        runs=runs,
    )
    requester = Actor(REQUESTER, "user", "cli")
    change_set_id = runner.run(runs.create_change_set("change", requester))
    run_id = runner.run(
        runs.create_run(change_set_id, device_id, "thread-1", requester)
    )
    state: ReviewState = {
        "run_id": run_id,
        "change_set_id": change_set_id,
        "requested_by": REQUESTER,
    }
    return World(deps, tmp_path, engine, state)


def start(runner: asyncio.Runner, world: World) -> Graph:
    """Run the collect graph until it ends, and return it."""
    graph = build_collect_graph(world.deps).compile(checkpointer=InMemorySaver())  # pyright: ignore[reportUnknownMemberType]
    runner.run(start_run(graph, world.state, "thread-1"))
    return graph


def snapshot(runner: asyncio.Runner, graph: Graph) -> StateSnapshot:
    """Return the checkpoint of the test thread."""
    config: RunnableConfig = {"configurable": {"thread_id": "thread-1"}}
    return runner.run(graph.aget_state(config))


def scalar_rows(engine: Engine, sql: str) -> list[tuple[object, ...]]:
    """Return the rows of a query."""
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql))]


def execute(engine: Engine, sql: str) -> None:
    """Run one statement."""
    with engine.begin() as connection:
        connection.execute(text(sql))


def run_row(world: World) -> tuple[object, ...]:
    """Return the state, the fingerprint, and the error of the run."""
    (row,) = scalar_rows(
        world.engine,
        "SELECT state, change_fingerprint, error FROM hco.remediation_run",
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


def snapshot_kinds(world: World) -> list[object]:
    """Return the kind of every snapshot, sorted."""
    rows = scalar_rows(world.engine, "SELECT kind FROM hco.config_snapshot ORDER BY 1")
    return [kind for (kind,) in rows]


def test_merge_dicts_keeps_the_keys_of_both_sides() -> None:
    """A later key wins, and every other key stays."""
    assert merge_dicts({"a": 1, "b": 1}, {"b": 2, "c": 3}) == {"a": 1, "b": 2, "c": 3}


def test_a_converged_change_passes_the_gate(
    runner: asyncio.Runner, world: World
) -> None:
    """The run reaches `REMEDIATING` with the bundle and the facts in the state."""
    graph = start(runner, world)

    done = snapshot(runner, graph)
    assert done.next == ()
    assert type(done.values["run_state"]) is str
    assert done.values["run_state"] == "REMEDIATING"
    assert done.values["plan_source"] == "deterministic"
    assert done.values["sot_device_id"] == "r1"
    assert done.values["future_report"]["unresolved_negations"] == []
    assert done.values["errors"] == {"collect_running": "", "build_remediation": ""}
    derived = done.values["evidence"]["config_derived"]
    assert derived["top_level_sections"] == ["ntp server 192.0.2.10"]
    state, fingerprint, error = run_row(world)
    assert (state, error) == ("REMEDIATING", None)
    assert fingerprint == done.values["change_fingerprint"]
    assert transitions(world) == ["DRAFT", "COLLECTING", "REMEDIATING"]


def test_the_snapshots_hold_the_three_configs(
    runner: asyncio.Runner, world: World
) -> None:
    """The run points at the running, the intended, and the future snapshot."""
    start(runner, world)

    rows = scalar_rows(
        world.engine,
        "SELECT s.kind, s.source, s.body_inline FROM hco.config_snapshot s "
        "JOIN hco.remediation_run r ON s.id IN "
        "(r.running_snapshot_id, r.intended_snapshot_id, r.future_snapshot_id) "
        "ORDER BY s.kind",
    )
    assert rows == [
        ("future", "sot", FUTURE),
        ("intended", "sot", INTENDED),
        ("running", "device", RUNNING),
    ]


def test_the_plugins_and_tag_rules_shape_the_remediation(
    runner: asyncio.Runner, world: World
) -> None:
    """The plugin factory and the tag rules of the dependencies reach the engine."""
    devices: list[str] = []

    def plugins(device: DeviceRecord) -> tuple[Callable[..., None], ...]:
        devices.append(device.name)

        def add_banner(remediation: HConfig) -> None:
            remediation.add_child("banner motd x")

        return (add_banner,)

    rule = {"match_rules": [{"startswith": "ntp"}], "apply_tags": ["safe"]}
    world.deps = replace(world.deps, plugins=plugins, tag_rules=(rule,))
    graph = start(runner, world)

    lines = snapshot(runner, graph).values["remediation_dump"]["lines"]
    tags = {line["text"]: line["tags"] for line in lines}
    assert tags == {"ntp server 192.0.2.10": ["safe"], "banner motd x": []}
    assert devices == ["edge-1"]


def test_a_second_start_on_the_thread_writes_nothing(
    runner: asyncio.Runner, world: World
) -> None:
    """A run that left `DRAFT` does not start again, and keeps its rows."""
    graph = start(runner, world)
    runner.run(start_run(graph, world.state, "thread-1"))

    assert run_row(world)[0] == "REMEDIATING"
    assert snapshot_kinds(world) == ["future", "intended", "running"]
    assert transitions(world) == ["DRAFT", "COLLECTING", "REMEDIATING"]


def test_a_blocked_run_stays_blocked(runner: asyncio.Runner, world: World) -> None:
    """A second start of a blocked run keeps the state and the first error."""
    (world.root / "intended.txt").write_text(UNRESOLVED, encoding="utf8")
    graph = start(runner, world)
    first = run_row(world)
    (world.root / "intended.txt").write_text(INTENDED, encoding="utf8")
    runner.run(start_run(graph, world.state, "thread-1"))

    assert run_row(world) == first
    assert transitions(world) == ["DRAFT", "COLLECTING", "REMEDIATING", "BLOCKED"]
    assert snapshot_kinds(world) == ["future", "intended", "running"]


def test_a_retried_node_writes_once(runner: asyncio.Runner, world: World) -> None:
    """A node that runs again after a crash writes no second row."""
    first = runner.run(review.collect_running(world.state, world.deps))
    again = runner.run(review.collect_running(world.state, world.deps))
    assert again == first
    state: ReviewState = {**world.state, **first}
    built = runner.run(review.build_remediation(state, world.deps))
    assert runner.run(review.build_remediation(state, world.deps)) == built

    assert snapshot_kinds(world) == ["future", "intended", "running"]
    assert transitions(world) == ["DRAFT", "COLLECTING", "REMEDIATING"]


class FailingSource(YamlSourceOfTruth):
    """A source of truth whose `get_device` raises."""

    def __init__(self, path: Path, error: Exception) -> None:
        super().__init__(path)
        self.error = error

    @override
    async def get_device(self, device_id: DeviceID) -> DeviceRecord:
        """Raise the error that the source holds."""
        raise self.error


class SwappingSource(YamlSourceOfTruth):
    """A source of truth that returns r2 for every device id."""

    @override
    async def get_device(self, device_id: DeviceID) -> DeviceRecord:
        """Return r2."""
        return await super().get_device(DeviceID("r2"))


def unresolved_negation(world: World, patch: pytest.MonkeyPatch) -> None:
    """Ask for a negation of a line that r1 does not have."""
    del patch
    (world.root / "intended.txt").write_text(UNRESOLVED, encoding="utf8")


def unknown_device(world: World, patch: pytest.MonkeyPatch) -> None:
    """Point the device row at an id that the inventory does not hold."""
    del patch
    execute(world.engine, "UPDATE hco.device SET sot_device_id = 'r9'")


def no_intent(world: World, patch: pytest.MonkeyPatch) -> None:
    """Point the device row at a device with no intended config."""
    del patch
    execute(world.engine, "UPDATE hco.device SET sot_device_id = 'r2'")


def sot_unreachable(world: World, patch: pytest.MonkeyPatch) -> None:
    """Make the source of truth fail with an adapter error."""
    del patch
    error = DeviceUnreachableError("the source of truth is unreachable")
    source = FailingSource(world.root / "inventory.yaml", error)
    world.deps = replace(world.deps, sot=source)


def sot_crash(world: World, patch: pytest.MonkeyPatch) -> None:
    """Make the source of truth fail with an error that no adapter declares."""
    del patch
    error = RuntimeError("the source of truth crashed")
    source = FailingSource(world.root / "inventory.yaml", error)
    world.deps = replace(world.deps, sot=source)


def wrong_device(world: World, patch: pytest.MonkeyPatch) -> None:
    """Make the source of truth return another device."""
    del patch
    source = SwappingSource(world.root / "inventory.yaml")
    world.deps = replace(world.deps, sot=source)


def platform_mismatch(world: World, patch: pytest.MonkeyPatch) -> None:
    """Give the device row another platform than the source of truth."""
    del patch
    execute(world.engine, "UPDATE hco.device SET platform_name = 'CISCO_NXOS'")


def unreachable(world: World, patch: pytest.MonkeyPatch) -> None:
    """Remove the transcripts of r1."""
    del patch
    (world.root / "transcripts" / "r1" / "running.txt").unlink()
    (world.root / "transcripts" / "r1").rmdir()


def nul_output(world: World, patch: pytest.MonkeyPatch) -> None:
    """Put a NUL character in the running config of r1."""
    del patch
    running = world.root / "transcripts" / "r1" / "running.txt"
    running.write_text("hostname r1\x00\n", encoding="utf8")


def driver_mismatch(world: World, patch: pytest.MonkeyPatch) -> None:
    """Make the engine raise as it does for two drivers."""
    del world

    def mismatch(*args: object, **kwargs: object) -> None:
        del args, kwargs
        message = "CISCO_IOS and CISCO_NXOS"
        raise DriverMismatchError(message)

    patch.setattr(review, "build_bundle", mismatch)


def plugin_error(world: World, patch: pytest.MonkeyPatch) -> None:
    """Give a plugin that raises a `KeyError`."""
    del patch

    def broken(remediation: HConfig) -> None:
        del remediation
        message = "plugin lookup"
        raise KeyError(message)

    def plugins(device: DeviceRecord) -> tuple[Callable[..., None], ...]:
        del device
        return (broken,)

    world.deps = replace(world.deps, plugins=plugins)


@pytest.mark.parametrize(
    ("arrange", "expected"),
    [
        (unresolved_negation, "unresolved negation"),
        (unknown_device, "no device 'r9'"),
        (no_intent, "no intended"),
        (sot_unreachable, "source of truth is unreachable"),
        (sot_crash, "source of truth crashed"),
        (wrong_device, "'r2'"),
        (platform_mismatch, "CISCO_NXOS"),
        (unreachable, "no transcript"),
        (nul_output, "NUL"),
        (driver_mismatch, "CISCO_NXOS"),
        (plugin_error, "plugin lookup"),
    ],
    ids=[
        "unresolved",
        "sot-no-device",
        "no-intent",
        "sot-unreachable",
        "sot-crash",
        "wrong-device",
        "platform-mismatch",
        "unreachable",
        "nul",
        "driver-mismatch",
        "plugin-error",
    ],
)
def test_a_failure_blocks_the_run(
    runner: asyncio.Runner,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Callable[[World, pytest.MonkeyPatch], object],
    expected: str,
) -> None:
    """The run ends `BLOCKED` with the error, and nothing after the gate runs (AC-4)."""
    arrange(world, monkeypatch)
    calls, graph = gate_counter(runner, world)

    assert snapshot(runner, graph).next == ()
    state, _, error = run_row(world)
    assert state == "BLOCKED"
    assert expected in str(error)
    assert transitions(world)[-1] == "BLOCKED"
    assert calls == []


def gate_counter(runner: asyncio.Runner, world: World) -> tuple[list[str], Graph]:
    """Run the collect nodes with a counter node at the gate exit."""
    calls: list[str] = []

    def after_gate(state: ReviewState) -> ReviewState:
        calls.append(str(state.get("run_state")))
        return {}

    graph = StateGraph(ReviewState)
    graph.add_node("after_gate", after_gate)  # pyright: ignore[reportUnknownMemberType]
    graph.add_edge("after_gate", END)
    add_collect_nodes(graph, world.deps, gate_passed="after_gate")
    compiled = graph.compile(checkpointer=InMemorySaver())  # pyright: ignore[reportUnknownMemberType]
    runner.run(start_run(compiled, world.state, "thread-1"))
    return calls, compiled


def test_the_gate_exit_is_a_seam(runner: asyncio.Runner, world: World) -> None:
    """A node at the gate exit runs only for a run that passes."""
    calls, _ = gate_counter(runner, world)
    assert calls == ["REMEDIATING"]


def test_a_node_names_a_missing_key(runner: asyncio.Runner, world: World) -> None:
    """A missing input raises with the key name, and nothing is written."""
    with pytest.raises(review.MissingStateError, match="'run_id'"):
        runner.run(review.collect_running({}, world.deps))
    assert snapshot_kinds(world) == []
