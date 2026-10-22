"""The run repository writes runs, change sets, snapshots, and their audit."""

import asyncio
import hashlib
from typing import Literal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.domain.approval import RunState
from hco_core.domain.remediation import DumpJson
from hco_core.domain.risk import RiskTier
from hco_core.services.audit import Actor
from hco_core.services.runs import (
    RunNotFoundError,
    RunRepository,
    RunStateError,
    RunUpdate,
    Snapshot,
)
from tests.conftest import REQUESTER

GRAPH = Actor("hco-core", "system", "api")
REQUESTER_ACTOR = Actor(REQUESTER, "user", "cli", request_id="req-1")
GRAPH_COLUMNS = ("system", "api", None)
REQUESTER_COLUMNS = ("user", "cli", "req-1")
DUMP: DumpJson = {"lines": []}
BODY = "hostname r1\n"


@pytest.fixture
def runs(sessions: async_sessionmaker[AsyncSession]) -> RunRepository:
    """Give a repository on the test database."""
    return RunRepository(sessions)


def audit_rows(engine: Engine) -> list[tuple[object, ...]]:
    """Return the actor, action, object, states, and caller of every event, in order."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT actor, action, object_type, object_id, before, after, "
                "actor_kind, surface, request_id "
                "FROM hco.audit_event ORDER BY occurred_at, id"
            )
        ).all()
    return [tuple(row) for row in rows]


def test_a_change_set_holds_its_requester(
    runner: asyncio.Runner, runs: RunRepository, engine: Engine
) -> None:
    """The requester is the actor who creates the change set."""
    change_set_id = runner.run(runs.create_change_set("change", REQUESTER_ACTOR))

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT requested_by, state, transaction_class FROM hco.change_set")
        ).one()
    assert tuple(row) == (REQUESTER, "DRAFT", "strict")
    assert audit_rows(engine) == [
        (
            REQUESTER,
            "change_set.create",
            "change_set",
            change_set_id,
            None,
            {"state": "DRAFT"},
            *REQUESTER_COLUMNS,
        )
    ]


def test_a_run_starts_in_draft_and_records_each_transition(
    runner: asyncio.Runner, runs: RunRepository, engine: Engine, device_id: str
) -> None:
    """Every state change writes one audit event with the before and after state."""
    change_set_id = runner.run(runs.create_change_set("change", REQUESTER_ACTOR))
    run_id = runner.run(runs.create_run(change_set_id, device_id, "thread-1", GRAPH))
    runner.run(runs.update_run(run_id, RunUpdate(state=RunState.COLLECTING), GRAPH))
    runner.run(
        runs.update_run(
            run_id,
            RunUpdate(
                state=RunState.AWAITING_APPROVAL,
                remediation_dump=DUMP,
                rollback_dump=DUMP,
                change_fingerprint="f" * 64,
                risk_tier_floor=RiskTier.MEDIUM,
                risk_tier_final=RiskTier.HIGH,
            ),
            GRAPH,
        )
    )

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, thread_id, remediation_dump, risk_tier_floor, "
                "risk_tier_final, change_fingerprint FROM hco.remediation_run"
            )
        ).one()
    assert tuple(row) == (
        "AWAITING_APPROVAL",
        "thread-1",
        DUMP,
        "MEDIUM",
        "HIGH",
        "f" * 64,
    )
    run_events = [
        event for event in audit_rows(engine) if event[2] == "remediation_run"
    ]
    assert run_events == [
        (
            "hco-core",
            "remediation_run.create",
            "remediation_run",
            run_id,
            None,
            {"state": "DRAFT"},
            *GRAPH_COLUMNS,
        ),
        (
            "hco-core",
            "remediation_run.transition",
            "remediation_run",
            run_id,
            {"state": "DRAFT"},
            {"state": "COLLECTING"},
            *GRAPH_COLUMNS,
        ),
        (
            "hco-core",
            "remediation_run.transition",
            "remediation_run",
            run_id,
            {"state": "COLLECTING"},
            {"state": "AWAITING_APPROVAL"},
            *GRAPH_COLUMNS,
        ),
    ]


def test_an_update_without_a_state_change_writes_no_audit_event(
    runner: asyncio.Runner, runs: RunRepository, engine: Engine, device_id: str
) -> None:
    """Only a transition is an audit event."""
    change_set_id = runner.run(runs.create_change_set("change", REQUESTER_ACTOR))
    run_id = runner.run(runs.create_run(change_set_id, device_id, "thread-1", GRAPH))
    runner.run(runs.update_run(run_id, RunUpdate(change_fingerprint="a" * 64), GRAPH))
    runner.run(runs.update_run(run_id, RunUpdate(state=RunState.DRAFT), GRAPH))

    assert [event[1] for event in audit_rows(engine)] == [
        "change_set.create",
        "remediation_run.create",
    ]


def test_a_change_set_transition_is_recorded(
    runner: asyncio.Runner, runs: RunRepository, engine: Engine
) -> None:
    """A change set state change writes one audit event."""
    change_set_id = runner.run(runs.create_change_set("change", REQUESTER_ACTOR))
    runner.run(runs.set_change_set_state(change_set_id, RunState.APPROVED, GRAPH))

    assert audit_rows(engine)[-1] == (
        "hco-core",
        "change_set.transition",
        "change_set",
        change_set_id,
        {"state": "DRAFT"},
        {"state": "APPROVED"},
        *GRAPH_COLUMNS,
    )


def test_an_unknown_run_or_change_set_is_named(
    runner: asyncio.Runner, runs: RunRepository, engine: Engine
) -> None:
    """An update of a row that does not exist raises a typed error."""
    with pytest.raises(RunNotFoundError, match="remediation run '01JNOSUCHRUN"):
        runner.run(
            runs.update_run(
                "01JNOSUCHRUN00000000000000", RunUpdate(state=RunState.BLOCKED), GRAPH
            )
        )
    with pytest.raises(RunNotFoundError, match="change set '01JNOSUCHSET"):
        runner.run(
            runs.set_change_set_state(
                "01JNOSUCHSET00000000000000", RunState.BLOCKED, GRAPH
            )
        )
    assert audit_rows(engine) == []


def test_a_snapshot_stores_the_body_digest_and_the_dump(
    runner: asyncio.Runner, runs: RunRepository, engine: Engine, device_id: str
) -> None:
    """The snapshot keeps the faithful text, its sha256, and the parsed dump."""
    snapshot_id = runner.run(
        runs.add_snapshot(
            Snapshot(device_id=device_id, kind="running", body=BODY, dump=DUMP)
        )
    )

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, kind, format, source, body_sha256, body_inline, dump "
                "FROM hco.config_snapshot"
            )
        ).one()
    assert tuple(row) == (
        snapshot_id,
        "running",
        "cli",
        "device",
        hashlib.sha256(BODY.encode()).hexdigest(),
        BODY,
        DUMP,
    )


def snapshot_of(
    device_id: str, kind: Literal["running", "intended", "future"]
) -> Snapshot:
    """Return an empty snapshot of the kind."""
    return Snapshot(device_id=device_id, kind=kind, body=BODY, dump=DUMP)


def test_the_guarded_writes_refuse_a_run_in_another_state(
    runner: asyncio.Runner, runs: RunRepository, device_id: str
) -> None:
    """Collection needs `DRAFT` or `COLLECTING`, and remediation needs `COLLECTING`."""
    change_set_id = runner.run(runs.create_change_set("change", REQUESTER_ACTOR))
    run_id = runner.run(runs.create_run(change_set_id, device_id, "thread-1", GRAPH))
    future = snapshot_of(device_id, "future")
    with pytest.raises(RunStateError, match="needs COLLECTING"):
        runner.run(runs.record_remediation(run_id, future, RunUpdate(), GRAPH))
    blocked = RunUpdate(state=RunState.BLOCKED)
    assert not runner.run(
        runs.update_run(run_id, blocked, GRAPH, only_from={RunState.COLLECTING})
    )
    assert runner.run(runs.update_run(run_id, blocked, GRAPH))
    running, intended = (
        snapshot_of(device_id, "running"),
        snapshot_of(device_id, "intended"),
    )
    with pytest.raises(RunStateError, match="needs DRAFT"):
        runner.run(runs.record_collection(run_id, running, intended, GRAPH))
    assert runner.run(runs.get_run(run_id)).state is RunState.BLOCKED
    with pytest.raises(RunNotFoundError):
        runner.run(runs.get_run("missing"))


def test_a_change_set_and_its_run_are_written_in_one_transaction(
    runner: asyncio.Runner, runs: RunRepository, engine: Engine, device_id: str
) -> None:
    """Both rows and both audit events are written, or none is."""
    change_set_id, run_id = runner.run(
        runs.create_change_set_and_run("change", device_id, "thread-1", REQUESTER_ACTOR)
    )

    with pytest.raises(IntegrityError):
        runner.run(
            runs.create_change_set_and_run(
                "change", "01JNODEVICE000000000000000", "thread-2", REQUESTER_ACTOR
            )
        )

    with engine.connect() as connection:
        change_sets = connection.execute(
            text("SELECT id FROM hco.change_set")
        ).scalars()
        assert list(change_sets) == [change_set_id]
    assert runner.run(runs.get_run(run_id)).thread_id == "thread-1"
    assert [row[1] for row in audit_rows(engine)] == [
        "change_set.create",
        "remediation_run.create",
    ]


def test_the_run_of_a_change_set_is_the_newest_and_none_is_named(
    runner: asyncio.Runner, runs: RunRepository, change_set_id: str, device_id: str
) -> None:
    """The lookup gives the newest run, and a change set with no run raises."""
    with pytest.raises(RunNotFoundError, match="change set"):
        runner.run(runs.run_of_change_set(change_set_id))
    actor = Actor(REQUESTER, "user", "api")
    runner.run(runs.create_run(change_set_id, device_id, "thread-1", actor))
    newest = runner.run(runs.create_run(change_set_id, device_id, "thread-2", actor))

    assert runner.run(runs.run_of_change_set(change_set_id)) == newest
