"""Open approval requests and record decisions, whatever surface calls.

The requester never counts. This service refuses the requester with a typed
error, and the `hco.reject_self_approval()` trigger refuses the same row again.
The service compares identities: two names with the same NFKC, case-folded form
are one person. The trigger compares exact names.
"""

import unicodedata
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
    """A name is empty, has white space at an end, or has a format character."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"the name {name!r} is empty, has white space at an end, "
            "or has an invisible format character"
        )


class RequesterMismatchError(ApprovalError, ValueError):
    """The requester differs from the one that the database holds."""


class QuorumMismatchError(ApprovalError, ValueError):
    """The open request needs fewer approvals than the caller asks for."""


class NaiveExpiryError(ApprovalError, ValueError):
    """The expiry has no time zone."""


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
        the existing request unchanged. The requester must match exactly,
        because it comes from `change_set.requested_by`.

        Raises:
            InvalidActorError: If the requester name is not valid.
            NaiveExpiryError: If `expires_at` has no time zone.
            RequesterMismatchError: If the requester is not the one that
                `change_set.requested_by` or the existing request holds.
            QuorumMismatchError: If the open request needs fewer approvals
                than this call asks for.

        """
        if not _is_valid_name(requester):
            raise InvalidActorError(requester)
        if expires_at.utcoffset() is None:
            message = f"the expiry {expires_at.isoformat()} has no time zone"
            raise NaiveExpiryError(message)
        quorum = max(required_count, APPROVALS_BY_TIER[tier])
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
                    required_count=quorum,
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
            if row.required_count < quorum:
                message = (
                    f"approval request {row.id!r} needs {row.required_count} "
                    f"approvals, and this call asks for {quorum}"
                )
                raise QuorumMismatchError(message)
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
        closes the request. If it raises, the decision stays recorded and the
        error reaches the caller.

        Raises:
            InvalidActorError: If the actor name is not valid.
            ApprovalRequestNotFoundError: If no request has the id.
            ApprovalClosedError: If the request is not pending, or expired.
            RequesterMayNotApproveError: If the actor is the requester.
            DuplicateDecisionError: If the actor already decided.

        """
        if not _is_valid_name(actor):
            raise InvalidActorError(actor)
        identity = _identity(actor)
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
            if identity == _identity(row.requester):
                message = f"requester {actor!r} may not decide on their own change"
                raise RequesterMayNotApproveError(message)
            if identity in await _decided_identities(session, request_id):
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
            approvals = await _approvals(session, request_id)
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


def _is_valid_name(name: str) -> bool:
    return (
        bool(name)
        and name == name.strip()
        and all(unicodedata.category(character) != "Cf" for character in name)
    )


def _identity(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


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


async def _decided_identities(session: AsyncSession, request_id: str) -> set[str]:
    actors = await session.scalars(
        select(models.ApprovalDecision.actor).where(
            models.ApprovalDecision.request_id == request_id
        )
    )
    return {_identity(actor) for actor in actors}


async def _approvals(session: AsyncSession, request_id: str) -> int:
    count = await session.scalar(
        select(func.count()).where(
            models.ApprovalDecision.request_id == request_id,
            models.ApprovalDecision.decision == Decision.APPROVE,
        )
    )
    return count or 0


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
