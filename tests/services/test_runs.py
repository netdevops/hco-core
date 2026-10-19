"""The run repository writes runs, change sets, snapshots, and their audit."""

import asyncio
import hashlib

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.domain.approval import RunState
from hco_core.domain.remediation import DumpJson
from hco_core.domain.risk import RiskTier
from hco_core.services.audit import Actor
from hco_core.services.runs import RunNotFoundError, RunRepository, RunUpdate, Snapshot
from tests.services.conftest import REQUESTER

GRAPH = Actor("hco-core", "system", "api")
REQUESTER_ACTOR = Actor(REQUESTER, "user", "cli")
DUMP: DumpJson = {"lines": []}
BODY = "hostname r1\n"


@pytest.fixture
def runs(sessions: async_sessionmaker[AsyncSession]) -> RunRepository:
    """Give a repository on the test database."""
    return RunRepository(sessions)


def audit_rows(engine: Engine) -> list[tuple[object, ...]]:
    """Return the action, object, and states of every audit event, in order."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT actor, action, object_type, object_id, before, after "
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
        ("hco-core", "run.create", "remediation_run", run_id, None, {"state": "DRAFT"}),
        (
            "hco-core",
            "run.transition",
            "remediation_run",
            run_id,
            {"state": "DRAFT"},
            {"state": "COLLECTING"},
        ),
        (
            "hco-core",
            "run.transition",
            "remediation_run",
            run_id,
            {"state": "COLLECTING"},
            {"state": "AWAITING_APPROVAL"},
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
        "run.create",
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
