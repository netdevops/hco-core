"""A run parked in Postgres resumes in a new graph with a new saver (AC-2, AC-3)."""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    CompiledStateGraph,
)
from langgraph.types import Command
from pydantic import SecretStr
from sqlalchemy import Engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.db.models import CHECKPOINT_TABLES, SCHEMA
from hco_core.db.types import new_id
from hco_core.domain.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalState,
    Decision,
)
from hco_core.domain.risk import RiskTier
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
from hco_core.services.approval import ApprovalClosedError, ApprovalService, Resume
from hco_core.services.audit import Actor
from hco_core.services.ledger import NotificationLedger
from hco_core.services.resume import (
    NotParkedError,
    RequestPendingError,
    RunResumer,
    review_with_resume,
)
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
    settings = CoreSettings()
    runner.run(setup_checkpointer(settings))
    return Parked(sessions, settings, engine, state, thread_id, tmp_path)


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


def resumer(parked: Parked, settings: CoreSettings | None = None) -> RunResumer:
    """Return a resumer on new dependencies."""
    deps = parked.deps()
    return RunResumer(
        RunRepository(parked.sessions),
        build_review_graph(deps),
        settings or parked.settings,
        approvals=deps.approvals,
    )


def park_and_approve(
    runner: asyncio.Runner, parked: Parked, run_resumer: RunResumer
) -> list[ApprovalDecision]:
    """Park the run, then close its request with a resume from the service."""
    run_id = str(parked.state.get("run_id"))
    closing: list[ApprovalDecision] = []

    async def wake(request: ApprovalRequest, decision: ApprovalDecision | None) -> None:
        assert request.state == "approved"
        assert decision is not None
        closing.append(decision)
        await run_resumer.resume(run_id, decision)

    runner.run(park(parked))
    runner.run(approve(parked, wake))
    return closing


def test_the_resumer_continues_the_run_after_the_request_closes(
    runner: asyncio.Runner, parked: Parked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service closes the request, then `RunResumer` resumes with sync durability."""
    durability: list[object] = []
    original = CompiledStateGraph.ainvoke  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    async def recording(self: Graph, *args: object, **kwargs: object) -> object:
        durability.append(kwargs.get("durability"))
        return await original(self, *args, **kwargs)  # pyright: ignore[reportUnknownVariableType, reportCallIssue, reportArgumentType]

    monkeypatch.setattr(CompiledStateGraph, "ainvoke", recording)
    (closing,) = park_and_approve(runner, parked, resumer(parked))

    assert closing.actor == "carol"
    assert durability == ["sync", "sync"]
    assert runner.run(next_nodes(parked)) == ()
    assert run_state(parked) == "APPROVED"


def test_a_second_resume_of_a_finished_thread_is_refused(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """A thread with no pending interrupt gets `NotParkedError`, and nothing is written."""
    run_resumer = resumer(parked)
    (closing,) = park_and_approve(runner, parked, run_resumer)
    run_id = str(parked.state.get("run_id"))

    with pytest.raises(NotParkedError, match=run_id):
        runner.run(run_resumer.resume(run_id, closing))

    assert run_state(parked) == "APPROVED"


def test_a_resume_before_the_run_starts_is_refused(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """A run with no checkpoint gets `NotParkedError`, and the graph does not start."""
    decision = ApprovalDecision(
        id=new_id(),
        request_id=new_id(),
        actor="bob",
        actor_kind="user",
        decision=Decision.APPROVE,
        surface="api",
        comment="",
    )

    with pytest.raises(NotParkedError):
        runner.run(resumer(parked).resume(str(parked.state.get("run_id")), decision))

    assert run_state(parked) == "DRAFT"
    assert runner.run(next_nodes(parked)) == ()


async def open_four(settings: CoreSettings) -> list[object]:
    """Open four savers at one time and read one checkpoint with each."""

    async def read() -> object:
        async with open_checkpointer(settings) as saver:
            return await saver.aget_tuple({"configurable": {"thread_id": "none"}})

    return list(await asyncio.gather(read(), read(), read(), read()))


def test_concurrent_opens_after_the_setup_pass(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """`open_checkpointer` makes no table, so concurrent opens do not race."""
    assert runner.run(open_four(parked.settings)) == [None, None, None, None]


APP_ROLE = "hco_core_app_test"


@pytest.fixture
def app_settings(engine: Engine, parked: Parked) -> Iterator[CoreSettings]:
    """Give settings for a role with DML grants on schema `hco` and no `CREATE`.

    Yields:
        The settings of the role. The role is dropped after the test.

    """
    del parked
    with engine.begin() as connection:
        connection.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = "
                f"'{APP_ROLE}') THEN CREATE ROLE {APP_ROLE} LOGIN PASSWORD "
                f"'{APP_ROLE}'; END IF; END $$"
            )
        )
        connection.execute(text(f"GRANT USAGE ON SCHEMA hco TO {APP_ROLE}"))
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hco "
                f"TO {APP_ROLE}"
            )
        )
    url = engine.url.set(username=APP_ROLE, password=APP_ROLE)
    yield CoreSettings(
        database_url=SecretStr(url.render_as_string(hide_password=False))
    )
    with engine.begin() as connection:
        connection.execute(text(f"DROP OWNED BY {APP_ROLE}"))
        connection.execute(text(f"DROP ROLE {APP_ROLE}"))


def test_a_role_with_no_create_grant_resumes_the_run(
    runner: asyncio.Runner, parked: Parked, app_settings: CoreSettings
) -> None:
    """The runtime role needs DML grants only, after `setup_checkpointer` ran."""
    park_and_approve(runner, parked, resumer(parked, app_settings))

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


def test_the_conninfo_keeps_the_options_of_the_url() -> None:
    """The search path is added to the options, so a statement timeout stays."""
    settings = CoreSettings(
        database_url=SecretStr(
            "postgresql://u:p@db/app?options=-c%20statement_timeout%3D5000"
        )
    )

    conninfo = checkpointer_conninfo(settings)

    assert "options='-c statement_timeout=5000 -c search_path=hco'" in conninfo


def test_a_malformed_url_error_does_not_show_the_password() -> None:
    """The error names the setting, never the URL or the password."""
    settings = CoreSettings(
        database_url=SecretStr("postgresql://hco:S3CRET pw@@127.0.0.1:55432/hco?x=1")
    )

    with pytest.raises(ValueError, match="HCO_DATABASE_URL") as raised:
        checkpointer_conninfo(settings)

    assert "S3CRET" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def wired(parked: Parked) -> ApprovalDeps:
    """Return new dependencies whose approval service resumes the run."""
    return review_with_resume(
        parked.settings,
        parked.sessions,
        lambda approvals: replace(parked.deps(), approvals=approvals),
    )


def request_row(parked: Parked) -> tuple[str, str]:
    """Return the id and the state of the one approval request."""
    ((request_id, state),) = scalar_rows(
        parked.engine, "SELECT id, state FROM hco.approval_request"
    )
    return str(request_id), str(state)


def test_a_resume_while_the_request_is_pending_is_refused(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """The run stays parked and its request stays pending, never `BLOCKED`."""
    runner.run(park(parked))
    request_id, _ = request_row(parked)
    early = ApprovalDecision(
        id=new_id(),
        request_id=request_id,
        actor="bob",
        actor_kind="user",
        decision=Decision.APPROVE,
        surface="api",
        comment="",
    )

    with pytest.raises(RequestPendingError, match=request_id):
        runner.run(resumer(parked).resume(str(parked.state.get("run_id")), early))

    assert run_state(parked) == "AWAITING_APPROVAL"
    assert request_row(parked) == (request_id, "pending")
    assert runner.run(next_nodes(parked)) == ("request_approval",)


def test_the_wired_service_resumes_the_run_when_the_quorum_closes_it(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """The first of two approvals leaves the run parked; the second resumes it."""
    runner.run(park(parked))
    request_id, _ = request_row(parked)
    approvals = wired(parked).approvals

    runner.run(approvals.decide(request_id, "bob", Decision.APPROVE, "api"))
    assert run_state(parked) == "AWAITING_APPROVAL"
    runner.run(approvals.decide(request_id, "carol", Decision.APPROVE, "api"))

    assert run_state(parked) == "APPROVED"
    assert runner.run(next_nodes(parked)) == ()


def test_a_cancel_resumes_the_parked_run_to_blocked(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """The graph reads the cancelled request and ends the run in `BLOCKED`."""
    runner.run(park(parked))
    request_id, _ = request_row(parked)

    runner.run(wired(parked).approvals.cancel(request_id, "bob", "api"))

    assert run_state(parked) == "BLOCKED"
    ((error,),) = scalar_rows(parked.engine, "SELECT error FROM hco.remediation_run")
    assert error == {"errors": ["record_decision: the approval request is cancelled"]}
    assert request_row(parked) == (request_id, ApprovalState.CANCELLED)


def test_a_cancel_of_a_run_that_is_not_parked_closes_only_the_request(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """A run with no pending interrupt keeps its state after a cancel."""
    approvals = wired(parked).approvals
    request = runner.run(
        approvals.upsert_request(
            str(parked.state.get("change_set_id")),
            RiskTier.LOW,
            1,
            REQUESTER,
            datetime.now(UTC) + timedelta(hours=1),
        )
    )

    runner.run(approvals.cancel(request.id, "bob", "api"))

    assert run_state(parked) == "DRAFT"
    assert request_row(parked) == (request.id, ApprovalState.CANCELLED)


def test_a_decision_on_a_run_that_is_not_parked_is_refused(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """The decision stays recorded, and `NotParkedError` reaches the caller."""
    approvals = wired(parked).approvals
    request = runner.run(
        approvals.upsert_request(
            str(parked.state.get("change_set_id")),
            RiskTier.LOW,
            1,
            REQUESTER,
            datetime.now(UTC) + timedelta(hours=1),
        )
    )

    with pytest.raises(NotParkedError):
        runner.run(approvals.decide(request.id, "bob", Decision.APPROVE, "api"))

    assert run_state(parked) == "DRAFT"
    assert request_row(parked) == (request.id, ApprovalState.APPROVED)


@pytest.mark.parametrize(
    "closers",
    [("decide", "cancel", "decide"), ("cancel", "cancel", "decide")],
    ids=["decide-first", "two-cancels"],
)
def test_concurrent_closers_close_once_and_resume_once(
    runner: asyncio.Runner,
    parked: Parked,
    monkeypatch: pytest.MonkeyPatch,
    closers: tuple[str, ...],
) -> None:
    """One closer wins, the others get `ApprovalClosedError`, and one resume runs."""
    resumed: list[str] = []
    original = RunResumer.resume_closed

    async def counted(
        self: RunResumer, request: ApprovalRequest, decision: ApprovalDecision | None
    ) -> None:
        resumed.append(request.state)
        await original(self, request, decision)

    monkeypatch.setattr(RunResumer, "resume_closed", counted)
    runner.run(park(parked))
    request_id, _ = request_row(parked)
    approvals = wired(parked).approvals
    runner.run(approvals.decide(request_id, "bob", Decision.APPROVE, "api"))
    actors = ("carol", "dave", "erin")

    async def race() -> list[ApprovalRequest | BaseException]:
        return await asyncio.gather(
            *(
                approvals.decide(request_id, actor, Decision.APPROVE, "api")
                if closer == "decide"
                else approvals.cancel(request_id, actor, "api")
                for closer, actor in zip(closers, actors, strict=True)
            ),
            return_exceptions=True,
        )

    outcomes = runner.run(race())

    winners = [item for item in outcomes if isinstance(item, ApprovalRequest)]
    losers = [item for item in outcomes if isinstance(item, ApprovalClosedError)]
    assert (len(winners), len(losers)) == (1, 2)
    assert resumed == [winners[0].state]
    assert (request_row(parked)[1], run_state(parked)) == {
        ApprovalState.APPROVED: ("approved", "APPROVED"),
        ApprovalState.CANCELLED: ("cancelled", "BLOCKED"),
    }[winners[0].state]


def test_a_cancel_closes_a_request_past_its_expiry(
    runner: asyncio.Runner, parked: Parked
) -> None:
    """Nothing else closes an expired request in Phase 1, so a cancel stops the run."""
    runner.run(park(parked))
    request_id, _ = request_row(parked)
    with parked.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE hco.approval_request SET expires_at = now() - interval '1 hour'"
            )
        )

    runner.run(wired(parked).approvals.cancel(request_id, "bob", "api"))

    assert request_row(parked) == (request_id, ApprovalState.CANCELLED)
    assert run_state(parked) == "BLOCKED"
