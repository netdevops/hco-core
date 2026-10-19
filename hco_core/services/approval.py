"""Open approval requests and record decisions, whatever surface calls.

The requester never counts. This service refuses the requester with a typed
error, and the `hco.reject_self_approval()` trigger refuses the same row again.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.db import models
from hco_core.db.types import new_id
from hco_core.domain.approval import (
    ActorKind,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalState,
    Decision,
    Surface,
)
from hco_core.domain.risk import RiskTier
from hco_core.policy.tiers import APPROVALS_BY_TIER
from hco_core.services.audit import Actor, AuditEntry, add_audit_event

type Resume = Callable[[ApprovalRequest, ApprovalDecision], Awaitable[None]]
"""Wake the parked run with the decision that closed its request."""


class ApprovalError(Exception):
    """The base of every refusal of the approval service."""


class InvalidActorError(ApprovalError, ValueError):
    """A name is empty or has white space at an end."""

    def __init__(self, name: str) -> None:
        super().__init__(f"the name {name!r} is empty or has white space at an end")


class RequesterMismatchError(ApprovalError, ValueError):
    """The requester differs from the one that the database holds."""


class ApprovalRequestNotFoundError(ApprovalError, LookupError):
    """No approval request has the id."""


class ApprovalClosedError(ApprovalError):
    """The request is no longer pending, or it expired."""


class RequesterMayNotApproveError(ApprovalError, PermissionError):
    """The requester tried to decide on their own change."""


class DuplicateDecisionError(ApprovalError):
    """The actor already decided on this request."""


class ApprovalService:
    """The one place that opens approval requests and records decisions."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, resume: Resume
    ) -> None:
        self._sessions = sessions
        self._resume = resume

    async def upsert_request(
        self,
        change_set_id: str,
        tier: RiskTier,
        required_count: int,
        requester: str,
        expires_at: datetime,
    ) -> ApprovalRequest:
        """Return the request of the change set, and open it on the first call.

        The count is never below the quorum of the tier. A later call returns
        the existing request unchanged.

        Raises:
            InvalidActorError: If the requester name is not trimmed.
            RequesterMismatchError: If the requester is not the one that
                `change_set.requested_by` or the existing request holds.

        """
        if not _is_trimmed(requester):
            raise InvalidActorError(requester)
        async with self._sessions.begin() as session:
            requested_by = await session.scalar(
                select(models.ChangeSet.requested_by).where(
                    models.ChangeSet.id == change_set_id
                )
            )
            if requested_by is None:
                message = f"no change set has the id {change_set_id!r}"
                raise RequesterMismatchError(message)
            _check_requester(requester, requested_by)
            request_id = new_id()
            await session.execute(
                insert(models.ApprovalRequest)
                .values(
                    id=request_id,
                    change_set_id=change_set_id,
                    state=ApprovalState.PENDING,
                    required_count=max(required_count, APPROVALS_BY_TIER[tier]),
                    requester=requester,
                    expires_at=expires_at,
                    interrupt_key=request_id,
                )
                .on_conflict_do_nothing(index_elements=["change_set_id"])
            )
            row = (
                await session.execute(
                    select(models.ApprovalRequest).where(
                        models.ApprovalRequest.change_set_id == change_set_id
                    )
                )
            ).scalar_one()
            _check_requester(requester, row.requester)
            return _to_request(row)

    async def decide(  # ruff: ignore[too-many-arguments]
        self,
        request_id: str,
        actor: str,
        decision: Decision,
        surface: Surface,
        comment: str = "",
        *,
        actor_kind: ActorKind = "user",
    ) -> ApprovalRequest:
        """Record one decision and close the request when it is decided.

        One rejection rejects. The approvals close the request when they reach
        `required_count`. The resume callback runs once, after the commit that
        closes the request.

        Raises:
            InvalidActorError: If the actor name is not trimmed.
            ApprovalRequestNotFoundError: If no request has the id.
            ApprovalClosedError: If the request is not pending, or expired.
            RequesterMayNotApproveError: If the actor is the requester.
            DuplicateDecisionError: If the actor already decided.

        """
        if not _is_trimmed(actor):
            raise InvalidActorError(actor)
        async with self._sessions.begin() as session:
            row = await session.get(
                models.ApprovalRequest, request_id, with_for_update=True
            )
            if row is None:
                message = f"no approval request has the id {request_id!r}"
                raise ApprovalRequestNotFoundError(message)
            closed = _closed_reason(row)
            if closed:
                raise ApprovalClosedError(closed)
            if actor == row.requester:
                message = f"requester {actor!r} may not decide on their own change"
                raise RequesterMayNotApproveError(message)
            if await _count(session, request_id, actor=actor):
                message = f"{actor!r} already decided on request {request_id!r}"
                raise DuplicateDecisionError(message)
            decision_row = models.ApprovalDecision(
                id=new_id(),
                request_id=request_id,
                actor=actor,
                actor_kind=actor_kind,
                decision=decision,
                surface=surface,
                comment=comment,
            )
            session.add(decision_row)
            await session.flush()
            approvals = await _count(session, request_id, decision=Decision.APPROVE)
            before = row.state
            row.state = _next_state(decision, approvals, row.required_count)
            add_audit_event(
                session,
                AuditEntry(
                    actor=Actor(actor, actor_kind, surface),
                    action="approval.decide",
                    object_type="approval_request",
                    object_id=request_id,
                    before={"state": before},
                    after={
                        "state": row.state,
                        "decision": decision.value,
                        "approvals": approvals,
                    },
                ),
            )
            request = _to_request(row)
            recorded = _to_decision(decision_row)
        if request.state is not ApprovalState.PENDING:
            await self._resume(request, recorded)
        return request


def _is_trimmed(name: str) -> bool:
    return bool(name) and name == name.strip()


def _check_requester(requester: str, recorded: str) -> None:
    if requester != recorded:
        message = (
            f"the requester {requester!r} is not the recorded requester {recorded!r}"
        )
        raise RequesterMismatchError(message)


def _closed_reason(row: models.ApprovalRequest) -> str:
    if row.state != ApprovalState.PENDING:
        return f"approval request {row.id!r} is {row.state}"
    if row.expires_at <= datetime.now(UTC):
        return f"approval request {row.id!r} expired at {row.expires_at.isoformat()}"
    return ""


async def _count(
    session: AsyncSession,
    request_id: str,
    *,
    actor: str | None = None,
    decision: Decision | None = None,
) -> int:
    query = select(func.count()).where(models.ApprovalDecision.request_id == request_id)
    if actor is not None:
        query = query.where(models.ApprovalDecision.actor == actor)
    if decision is not None:
        query = query.where(models.ApprovalDecision.decision == decision)
    return await session.scalar(query) or 0


def _next_state(decision: Decision, approvals: int, required_count: int) -> str:
    if decision is Decision.REJECT:
        return ApprovalState.REJECTED
    if approvals >= required_count:
        return ApprovalState.APPROVED
    return ApprovalState.PENDING


def _to_request(row: models.ApprovalRequest) -> ApprovalRequest:
    return ApprovalRequest.model_validate(row, from_attributes=True)


def _to_decision(row: models.ApprovalDecision) -> ApprovalDecision:
    return ApprovalDecision.model_validate(row, from_attributes=True)
