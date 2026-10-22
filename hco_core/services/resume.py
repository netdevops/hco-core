"""Resume a parked run after its approval request closes.

This is the only way a parked run continues. The resume payload never
approves: `record_decision` reads the closed request from the database.
"""

from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Final, cast

from langgraph.graph import StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.domain.approval import ApprovalDecision, ApprovalRequest, ApprovalState
from hco_core.graphs.checkpointer import open_checkpointer
from hco_core.graphs.review import (
    DURABILITY,
    GRAPH_ACTOR,
    ApprovalDeps,
    AssessmentHook,
    ReviewState,
    build_review_graph,
)
from hco_core.services.approval import ApprovalService
from hco_core.services.runs import RunRepository
from hco_core.settings import CoreSettings

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

CANCEL_PAYLOAD: Final = {
    "decision": "reject",
    "actor": GRAPH_ACTOR.name,
    "comment": "the approval request was cancelled",
}
"""The resume payload of a cancel. `record_decision` reads the request and blocks the run.

The "reject" only passes the payload schema of the graph and never decides.
An optional `_Resume.decision` in the review graph is the cleaner fix, deferred.
"""


class NotParkedError(RuntimeError):
    """The thread of the run has no pending interrupt to resume."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} is not parked for approval")
        self.run_id = run_id


class RequestPendingError(RuntimeError):
    """The approval request of the parked run is still pending."""

    def __init__(self, run_id: str, request_id: str) -> None:
        super().__init__(
            f"run {run_id!r} waits for approval request {request_id!r}, "
            "which is still pending"
        )


class RunResumer:
    """Resume the thread of a run with a new saver and a new compiled graph."""

    def __init__(
        self,
        runs: RunRepository,
        graph: StateGraph[ReviewState],
        settings: CoreSettings,
        *,
        approvals: ApprovalService,
    ) -> None:
        self._runs = runs
        self._graph = graph
        self._settings = settings
        self._approvals = approvals

    async def resume(self, run_id: str, decision: ApprovalDecision) -> None:
        """Resume the run on its thread with the decision, until it ends or parks.

        An unknown run id raises `RunNotFoundError`. A thread with no pending
        interrupt raises `NotParkedError`, and a pending request of the run
        raises `RequestPendingError`; the run then stays parked.
        """
        payload = decision.model_dump(
            mode="json", include={"decision", "actor", "comment"}
        )
        await self._resume(run_id, payload)

    async def resume_closed(
        self, request: ApprovalRequest, decision: ApprovalDecision | None
    ) -> None:
        """Resume the run of a closed request. The approval service calls it once.

        After a cancel, a parked run ends in `BLOCKED`, and a run that is not
        parked keeps its state. After a decision, a run that is not parked
        raises `NotParkedError`.
        """
        run_id = await self._runs.run_of_change_set(request.change_set_id)
        if decision is not None:
            await self.resume(run_id, decision)
            return
        with suppress(NotParkedError):
            await self._resume(run_id, CANCEL_PAYLOAD)

    async def _resume(self, run_id: str, payload: Mapping[str, str]) -> None:
        """Resume the parked thread of the run, when its request is closed.

        Raises:
            NotParkedError: If the thread has no pending interrupt.
            RequestPendingError: If the request of the run is still pending.

        """
        thread_id = (await self._runs.get_run(run_id)).thread_id
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        async with open_checkpointer(self._settings) as saver:
            graph = self._graph.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
            interrupts = (await graph.aget_state(config)).interrupts
            if not interrupts:
                raise NotParkedError(run_id)
            parked_on = cast("Mapping[str, object]", interrupts[0].value)
            request, _ = await self._approvals.get_request(str(parked_on["request_id"]))
            if request.state is ApprovalState.PENDING:
                raise RequestPendingError(run_id, request.id)
            await graph.ainvoke(Command(resume=payload), config, durability=DURABILITY)  # pyright: ignore[reportUnknownMemberType]


def review_with_resume(
    settings: CoreSettings,
    sessions: async_sessionmaker[AsyncSession],
    review: Callable[[ApprovalService], ApprovalDeps],
    assessment: AssessmentHook | None = None,
) -> ApprovalDeps:
    """Return the review dependencies with an approval service that resumes runs.

    Each closed request resumes its run once, after the commit that closes it.
    Give the hook that starts the runs, so that the two graphs match.
    """

    async def resume(
        request: ApprovalRequest, decision: ApprovalDecision | None
    ) -> None:
        await resumer.resume_closed(request, decision)

    approvals = ApprovalService(sessions, resume=resume)
    deps = review(approvals)
    resumer = RunResumer(
        deps.runs,
        build_review_graph(deps, assessment=assessment),
        settings,
        approvals=approvals,
    )
    return deps
