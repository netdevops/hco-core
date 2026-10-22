"""The approval routes: list and show requests, decide, and cancel.

`POST /approvals/{id}/decide` is the one place where the API approves a change.
It calls `ApprovalService.decide`, and the service resumes the run after the
commit that closes the request. The route returns when the run is parked again
or has ended, so the response shows the run state. No route here sets a run
state or writes to a device.
"""

import re
from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select

from hco_core.api.actor import current_actor
from hco_core.api.deps import DEFAULT_PAGE, ULID_PATTERN, ApiDeps, Cursor, Limit
from hco_core.api.errors import database_errors
from hco_core.db import models
from hco_core.domain.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalState,
    Decision,
    RunState,
)
from hco_core.domain.base import BaseModel
from hco_core.services.approval import ApprovalRequestNotFoundError
from hco_core.services.audit import Actor

MAX_COMMENT: Final = 4000
_ULID: Final = re.compile(ULID_PATTERN)


type Comment = Annotated[str, Field(max_length=MAX_COMMENT)]
"""Why the actor decides or cancels."""


class CancelBody(BaseModel):
    """Why the actor cancels the request."""

    comment: Comment = ""


class DecideBody(CancelBody):
    """The decision of the actor in `X-HCO-Actor`, and a comment."""

    decision: Decision


class ApprovalSummary(BaseModel):
    """One approval request and the run that waits for it."""

    id: str
    change_set_id: str
    run_id: str
    state: ApprovalState
    required_count: int
    requester: str
    expires_at: datetime


class ApprovalDetail(ApprovalSummary):
    """One approval request with its decisions, and the state of its run."""

    run_state: RunState
    decisions: tuple[ApprovalDecision, ...]


class ApprovalPage(BaseModel):
    """One page of pending requests, newest first, and the cursor of the next page."""

    items: tuple[ApprovalSummary, ...]
    next_cursor: str | None


def approvals_router(deps: ApiDeps) -> APIRouter:
    """Return the router of `/approvals`."""
    router = APIRouter(prefix="/approvals", tags=["approvals"])
    service = deps.review.approvals

    @router.get("")
    async def list_approvals(  # pyright: ignore[reportUnusedFunction]
        limit: Limit = DEFAULT_PAGE, cursor: Cursor = None
    ) -> ApprovalPage:
        """Return one page of the pending requests, newest first."""
        query = (
            select(models.ApprovalRequest, models.RemediationRun.id)
            .join(
                models.RemediationRun,
                models.RemediationRun.change_set_id
                == models.ApprovalRequest.change_set_id,
            )
            .where(models.ApprovalRequest.state == ApprovalState.PENDING)
            .order_by(models.ApprovalRequest.id.desc())
            .limit(limit + 1)
        )
        if cursor is not None:
            query = query.where(models.ApprovalRequest.id < cursor)
        with database_errors():
            async with deps.sessions() as session:
                rows = (await session.execute(query)).all()
        items = tuple(
            _summary(ApprovalRequest.model_validate(row, from_attributes=True), run_id)
            for row, run_id in rows[:limit]
        )
        return ApprovalPage(
            items=items, next_cursor=items[-1].id if len(rows) > limit else None
        )

    @router.get("/{request_id}")
    async def show_approval(request_id: str) -> ApprovalDetail:  # pyright: ignore[reportUnusedFunction]
        """Return the request, its decisions, and the state of its run."""
        _check_id(request_id)
        return await _detail(deps, request_id)

    @router.post("/{request_id}/decide")
    async def decide(  # pyright: ignore[reportUnusedFunction]
        request_id: str,
        body: DecideBody,
        actor: Annotated[Actor, Depends(current_actor)],
    ) -> ApprovalDetail:
        """Record the decision; a decision that closes the request resumes the run."""
        _check_id(request_id)
        with database_errors():
            await service.decide(
                request_id, actor.name, body.decision, "api", body.comment
            )
        return await _detail(deps, request_id)

    @router.post("/{request_id}/cancel")
    async def cancel(  # pyright: ignore[reportUnusedFunction]
        request_id: str,
        actor: Annotated[Actor, Depends(current_actor)],
        body: CancelBody | None = None,
    ) -> ApprovalDetail:
        """Close the pending request with no decision. A parked run ends `BLOCKED`."""
        _check_id(request_id)
        with database_errors():
            await service.cancel(
                request_id, actor.name, "api", "" if body is None else body.comment
            )
        return await _detail(deps, request_id)

    return router


async def _detail(deps: ApiDeps, request_id: str) -> ApprovalDetail:
    runs = deps.review.runs
    with database_errors():
        request, decisions = await deps.review.approvals.get_request(request_id)
        run_id = await runs.run_of_change_set(request.change_set_id)
        run = await runs.get_run(run_id)
    return ApprovalDetail(
        **request.model_dump(exclude={"interrupt_key"}),
        run_id=run_id,
        run_state=run.state,
        decisions=decisions,
    )


def _check_id(request_id: str) -> None:
    """Refuse an id that no request can have, before any query.

    Raises:
        ApprovalRequestNotFoundError: If the id is not a ULID.

    """
    if _ULID.fullmatch(request_id) is None:
        message = f"no approval request has the id {request_id!r}"
        raise ApprovalRequestNotFoundError(message)


def _summary(request: ApprovalRequest, run_id: str) -> ApprovalSummary:
    return ApprovalSummary(
        **request.model_dump(exclude={"interrupt_key"}), run_id=run_id
    )
