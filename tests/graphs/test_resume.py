"""A run parked in Postgres resumes in a new graph with a new saver (AC-2, AC-3)."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from langgraph.types import Command
from pydantic import SecretStr
from sqlalchemy import Engine, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.db.models import CHECKPOINT_TABLES, SCHEMA
from hco_core.db.types import new_id
from hco_core.domain.approval import ApprovalDecision, ApprovalRequest, Decision
from hco_core.graphs import review
from hco_core.graphs.checkpointer import (
    checkpointer_conninfo,
    open_checkpointer,
    setup_checkpointer,
)
from hco_core.graphs.review import (
    DURABILITY,
    ApprovalDeps,
    ReviewState,
    build_review_graph,
    start_run,
)
from hco_core.services.approval import ApprovalService, Resume
from hco_core.services.audit import Actor
from hco_core.services.ledger import NotificationLedger
from hco_core.services.resume import RunResumer
from hco_core.services.runs import RunRepository
from hco_core.settings import CoreSettings
from tests.conftest import REQUESTER
from tests.graphs.test_review import (
    APPROVE,
    ENVIRON,
    INTENDED,
    INVENTORY,
    RUNNING,
    Graph,
    RecordingChannel,
    no_resume,
    scalar_rows,
)

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


@dataclass
class Parked:
    """The services, the settings, and one run of a test."""

    sessions: async_sessionmaker[AsyncSession]
    settings: CoreSettings
    engine: Engine
    state: ReviewState
    thread_id: str
    root: Path

    def deps(self, resume: Resume = no_resume) -> ApprovalDeps:
        """Return new dependencies, as a new process builds them."""
        runs = RunRepository(self.sessions)
        return ApprovalDeps(
            sot=YamlSourceOfTruth(self.root / "inventory.yaml"),
            secrets=EnvSecretsProvider(ENVIRON),
            connection=MockConnectionDriver(
                self.root / "transcripts", token_key=b"k" * 32
            ),
            runs=runs,
            approvals=ApprovalService(self.sessions, resume=resume),
            ledger=NotificationLedger(self.sessions, {"test": RecordingChannel()}),
            channels=("test",),
        )

    def config(self) -> "RunnableConfig":
        """Return the config of the run thread."""
        return {"configurable": {"thread_id": self.thread_id}}


@pytest.fixture
def parked(
    runner: asyncio.Runner,
    sessions: async_sessionmaker[AsyncSession],
    engine: Engine,
    device_id: str,
    tmp_path: Path,
) -> Parked:
    """Give a run in `DRAFT` on a new thread, and its inventory."""
    (tmp_path / "inventory.yaml").write_text(INVENTORY, encoding="utf8")
    (tmp_path / "intended.txt").write_text(INTENDED, encoding="utf8")
    (tmp_path / "transcripts" / "r1").mkdir(parents=True)
    (tmp_path / "transcripts" / "r1" / "running.txt").write_text(RUNNING, "utf8")
    runs = RunRepository(sessions)
    requester = Actor(REQUESTER, "user", "cli")
    thread_id = f"thread-{new_id()}"
    change_set_id = runner.run(runs.create_change_set("change", requester))
    run_id = runner.run(runs.create_run(change_set_id, device_id, thread_id, requester))
    state: ReviewState = {
        "run_id": run_id,
        "change_set_id": change_set_id,
        "requested_by": REQUESTER,
    }
    return Parked(sessions, CoreSettings(), engine, state, thread_id, tmp_path)


@asynccontextmanager
async def new_graph(parked: Parked) -> AsyncGenerator[Graph]:
    """Give a new review graph on a new saver, as a new process builds it.

    Yields:
        The compiled graph. Its saver closes when the context ends.

    """
    async with open_checkpointer(parked.settings) as saver:
        yield build_review_graph(parked.deps()).compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]


async def park(parked: Parked) -> tuple[str, ...]:
    """Run the review graph with saver 1 and graph 1 until it parks."""
    async with new_graph(parked) as graph:
        await start_run(graph, parked.state, parked.thread_id)
        return (await graph.aget_state(parked.config())).next


async def approve(parked: Parked, resume: Resume = no_resume) -> None:
    """Close the one approval request with two approvals."""
    approvals = ApprovalService(parked.sessions, resume=resume)
    (request,) = scalar_rows(parked.engine, "SELECT id FROM hco.approval_request")
    for actor in ("bob", "carol"):
        await approvals.decide(str(request[0]), actor, Decision.APPROVE, "api")


async def resume_in_a_new_graph(parked: Parked) -> None:
    """Resume the thread with saver 2 and graph 2, as a new process does."""
    command = Command(resume=APPROVE)
    async with new_graph(parked) as graph:
        await graph.ainvoke(command, parked.config(), durability=DURABILITY)  # pyright: ignore[reportUnknownMemberType]


async def next_nodes(parked: Parked) -> tuple[str, ...]:
    """Return the next nodes of the thread, read with saver 3."""
    async with new_graph(parked) as graph:
        return (await graph.aget_state(parked.config())).next


def run_state(parked: Parked) -> object:
    """Return the state of the run row."""
    ((state,),) = scalar_rows(parked.engine, "SELECT state FROM hco.remediation_run")
    return state


def test_a_new_graph_and_a_new_saver_resume_the_parked_run(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """The thread survives the saver that wrote it, and reaches `APPROVED` (AC-2)."""
    assert runner.run(park(parked)) == ("request_approval",)
    assert run_state(parked) == "AWAITING_APPROVAL"

    runner.run(approve(parked))
    runner.run(resume_in_a_new_graph(parked))

    assert runner.run(next_nodes(parked)) == ()
    assert run_state(parked) == "APPROVED"


def test_park_and_resume_run_request_approval_twice_with_one_request_and_message(
    runner: asyncio.Runner, parked: Parked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`request_approval` runs twice, and its writes happen once (AC-3)."""
    runs: list[str] = []
    original = review.request_approval

    async def counted(state: ReviewState, deps: ApprovalDeps) -> ReviewState:
        runs.append(str(state.get("run_id")))
        return await original(state, deps)

    monkeypatch.setattr(review, "request_approval", counted)
    runner.run(park(parked))
    runner.run(approve(parked))
    runner.run(resume_in_a_new_graph(parked))

    assert len(runs) == 2  # ruff: ignore[magic-value-comparison]
    assert len(scalar_rows(parked.engine, "SELECT id FROM hco.approval_request")) == 1
    pending = scalar_rows(
        parked.engine,
        "SELECT id FROM hco.notification_delivery WHERE purpose = 'pending'",
    )
    assert len(pending) == 1
    assert run_state(parked) == "APPROVED"


def test_the_resumer_continues_the_run_after_the_request_closes(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """The service closes the request, then `RunResumer` resumes the thread of the run."""
    run_id = str(parked.state.get("run_id"))
    resumer = RunResumer(
        RunRepository(parked.sessions),
        build_review_graph(parked.deps()),
        parked.settings,
    )
    seen: list[str] = []

    async def wake(request: ApprovalRequest, decision: ApprovalDecision) -> None:
        seen.append(request.state)
        await resumer.resume(run_id, decision)

    runner.run(park(parked))
    runner.run(approve(parked, wake))

    assert seen == ["approved"]
    assert runner.run(next_nodes(parked)) == ()
    assert run_state(parked) == "APPROVED"


def test_the_saver_keeps_its_tables_in_the_hco_schema(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """`setup_checkpointer` makes the four tables in schema `hco`, and runs again safely."""
    runner.run(setup_checkpointer(parked.settings))
    runner.run(setup_checkpointer(parked.settings))

    tables = set(inspect(parked.engine).get_table_names(schema=SCHEMA))
    assert tables >= CHECKPOINT_TABLES


def test_the_conninfo_is_plain_libpq_with_the_hco_search_path() -> None:
    """The SQLAlchemy dialect is gone, and the saver works in schema `hco`."""
    settings = CoreSettings(
        database_url=SecretStr("postgresql://u:p@db:6543/app?sslmode=require")
    )

    conninfo = checkpointer_conninfo(settings)

    assert "psycopg" not in conninfo
    assert "host=db" in conninfo
    assert "sslmode=require" in conninfo
    assert "options='-c search_path=hco'" in conninfo
