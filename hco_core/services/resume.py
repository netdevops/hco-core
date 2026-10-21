"""Resume a parked run after its approval request closes.

This is the only way a parked run continues. The resume payload never
approves: `record_decision` reads the closed request from the database.
"""

from typing import TYPE_CHECKING

from langgraph.graph import StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command

from hco_core.domain.approval import ApprovalDecision
from hco_core.graphs.checkpointer import open_checkpointer
from hco_core.graphs.review import DURABILITY, ReviewState
from hco_core.services.runs import RunRepository
from hco_core.settings import CoreSettings

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


class RunResumer:
    """Resume the thread of a run with a new saver and a new compiled graph."""

    def __init__(
        self,
        runs: RunRepository,
        graph: StateGraph[ReviewState],
        settings: CoreSettings,
    ) -> None:
        self._runs = runs
        self._graph = graph
        self._settings = settings

    async def resume(self, run_id: str, decision: ApprovalDecision) -> None:
        """Resume the run on its thread with the decision, until it ends or parks.

        Call it only after the approval service closes the request. A run id
        that does not exist raises `RunNotFoundError`.
        """
        thread_id = (await self._runs.get_run(run_id)).thread_id
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        payload = {
            "decision": decision.decision.value,
            "actor": decision.actor,
            "comment": decision.comment,
        }
        async with open_checkpointer(self._settings) as saver:
            graph = self._graph.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
            await graph.ainvoke(Command(resume=payload), config, durability=DURABILITY)  # pyright: ignore[reportUnknownMemberType]
