"""The approval service decides by quorum, and the requester never counts (AC-7)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.domain.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalState,
    Decision,
    RunState,
)
from hco_core.domain.risk import RiskTier
from hco_core.policy.tiers import APPROVALS_BY_TIER, REQUESTER_MAY_APPROVE
from hco_core.services.approval import (
    ApprovalClosedError,
    ApprovalRequestNotFoundError,
    ApprovalService,
    DuplicateDecisionError,
    InvalidActorError,
    RequesterMayNotApproveError,
    RequesterMismatchError,
)
from tests.services.conftest import REQUESTER

LATER = datetime.now(UTC) + timedelta(hours=72)

type Resumed = list[tuple[ApprovalRequest, ApprovalDecision]]


@pytest.fixture
def resumed() -> Resumed:
    """Give the list that the resume callback appends to."""
    return []


@pytest.fixture
def service(
    sessions: async_sessionmaker[AsyncSession], resumed: Resumed
) -> ApprovalService:
    """Give a service whose resume callback records each call."""

    async def resume(request: ApprovalRequest, decision: ApprovalDecision) -> None:
        await asyncio.sleep(0)
        resumed.append((request, decision))

    return ApprovalService(sessions, resume=resume)


def open_request(
    runner: asyncio.Runner,
    service: ApprovalService,
    change_set_id: str,
    tier: RiskTier = RiskTier.LOW,
) -> ApprovalRequest:
    """Open the approval request of the change set at the tier."""
    return runner.run(service.upsert_request(change_set_id, tier, 1, REQUESTER, LATER))


def test_the_tier_table_is_the_phase_1_stand_in() -> None:
    """Two approvals from HIGH up, one below, and the requester never approves."""
    assert dict(APPROVALS_BY_TIER) == {
        RiskTier.LOW: 1,
        RiskTier.MEDIUM: 1,
        RiskTier.HIGH: 2,
        RiskTier.CRITICAL: 2,
    }
    assert REQUESTER_MAY_APPROVE is False


def test_the_run_states_match_the_database() -> None:
    """The enum holds the same values as the CHECK on remediation_run.state."""
    from hco_core.db.models import RUN_STATES  # ruff: ignore[import-outside-top-level]

    assert tuple(RunState) == RUN_STATES


def test_upsert_is_idempotent_on_the_change_set(
    runner: asyncio.Runner, service: ApprovalService, change_set_id: str
) -> None:
    """A second call returns the first request and adds no row."""
    first = open_request(runner, service, change_set_id)
    second = runner.run(
        service.upsert_request(
            change_set_id, RiskTier.LOW, 1, REQUESTER, LATER + timedelta(hours=1)
        )
    )

    assert second == first
    assert first.state is ApprovalState.PENDING
    assert first.requester == REQUESTER
    assert first.interrupt_key == first.id


def test_upsert_takes_at_least_the_quorum_of_the_tier(
    runner: asyncio.Runner, service: ApprovalService, change_set_id: str
) -> None:
    """A caller can raise the count and cannot lower it below the tier."""
    request = open_request(runner, service, change_set_id, RiskTier.HIGH)

    assert request.required_count == APPROVALS_BY_TIER[RiskTier.HIGH]


def test_upsert_refuses_a_requester_other_than_the_change_set_requester(
    runner: asyncio.Runner, service: ApprovalService, change_set_id: str
) -> None:
    """The requester comes from change_set.requested_by and cannot differ."""
    with pytest.raises(RequesterMismatchError, match="mallory"):
        runner.run(
            service.upsert_request(change_set_id, RiskTier.LOW, 1, "mallory", LATER)
        )


def test_upsert_never_changes_the_requester_of_a_request(
    runner: asyncio.Runner,
    service: ApprovalService,
    change_set_id: str,
    engine: Engine,
) -> None:
    """A changed change set requester does not reach an open request."""
    open_request(runner, service, change_set_id)
    with engine.begin() as connection:
        connection.execute(text("UPDATE hco.change_set SET requested_by = 'carol'"))

    with pytest.raises(RequesterMismatchError, match="carol"):
        runner.run(
            service.upsert_request(change_set_id, RiskTier.LOW, 1, "carol", LATER)
        )


def test_upsert_refuses_an_unknown_change_set(
    runner: asyncio.Runner, service: ApprovalService, change_set_id: str
) -> None:
    """A change set that does not exist has no requester to copy."""
    del change_set_id
    with pytest.raises(RequesterMismatchError, match="no change set"):
        runner.run(
            service.upsert_request(
                "01JNOSUCHCHANGESET00000000", RiskTier.LOW, 1, REQUESTER, LATER
            )
        )


def test_the_requester_may_not_approve(
    runner: asyncio.Runner,
    service: ApprovalService,
    change_set_id: str,
    resumed: Resumed,
) -> None:
    """The service refuses the requester before the database trigger does."""
    request = open_request(runner, service, change_set_id)

    with pytest.raises(RequesterMayNotApproveError, match=REQUESTER):
        runner.run(service.decide(request.id, REQUESTER, Decision.APPROVE, "api", ""))
    assert resumed == []


@pytest.mark.parametrize("actor", ["", " bob", "bob ", "bob\u2003", "bob\u00a0"])
def test_an_actor_with_white_space_at_an_end_is_refused(
    runner: asyncio.Runner, service: ApprovalService, change_set_id: str, actor: str
) -> None:
    """A padded name cannot pass as a different person."""
    request = open_request(runner, service, change_set_id)

    with pytest.raises(InvalidActorError):
        runner.run(service.decide(request.id, actor, Decision.APPROVE, "api", ""))


def test_a_second_decision_by_the_same_actor_is_refused(
    runner: asyncio.Runner, service: ApprovalService, change_set_id: str
) -> None:
    """One person cannot fill a two-approver quorum alone."""
    request = open_request(runner, service, change_set_id, RiskTier.HIGH)
    runner.run(service.decide(request.id, "bob", Decision.APPROVE, "api", ""))

    with pytest.raises(DuplicateDecisionError, match="bob"):
        runner.run(service.decide(request.id, "bob", Decision.APPROVE, "cli", ""))


@pytest.mark.parametrize(
    ("tier", "approvers"),
    [
        (RiskTier.LOW, ("bob",)),
        (RiskTier.MEDIUM, ("bob",)),
        (RiskTier.HIGH, ("bob", "carol")),
        (RiskTier.CRITICAL, ("bob", "carol")),
    ],
)
def test_the_quorum_of_the_tier_approves_and_resumes_once(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    runner: asyncio.Runner,
    service: ApprovalService,
    change_set_id: str,
    resumed: Resumed,
    tier: RiskTier,
    approvers: tuple[str, ...],
) -> None:
    """The request stays pending until the last approval of the quorum."""
    request = open_request(runner, service, change_set_id, tier)
    states = [
        runner.run(
            service.decide(request.id, actor, Decision.APPROVE, "api", "ok")
        ).state
        for actor in approvers
    ]

    assert states[-1] is ApprovalState.APPROVED
    assert all(state is ApprovalState.PENDING for state in states[:-1])
    assert len(resumed) == 1
    closed, decision = resumed[0]
    assert closed.state is ApprovalState.APPROVED
    assert decision.actor == approvers[-1]
    assert decision.comment == "ok"


def test_one_rejection_rejects_and_resumes(
    runner: asyncio.Runner,
    service: ApprovalService,
    change_set_id: str,
    resumed: Resumed,
) -> None:
    """A reject closes the request at any tier."""
    request = open_request(runner, service, change_set_id, RiskTier.HIGH)

    closed = runner.run(service.decide(request.id, "bob", Decision.REJECT, "ui", "no"))

    assert closed.state is ApprovalState.REJECTED
    assert [decision.decision for _, decision in resumed] == [Decision.REJECT]


def test_a_closed_request_takes_no_more_decisions(
    runner: asyncio.Runner,
    service: ApprovalService,
    change_set_id: str,
    resumed: Resumed,
) -> None:
    """A late decision neither counts nor resumes the run again."""
    request = open_request(runner, service, change_set_id)
    runner.run(service.decide(request.id, "bob", Decision.APPROVE, "api", ""))

    with pytest.raises(ApprovalClosedError, match="approved"):
        runner.run(service.decide(request.id, "carol", Decision.APPROVE, "api", ""))
    assert len(resumed) == 1


def test_an_expired_request_takes_no_decision(
    runner: asyncio.Runner, service: ApprovalService, change_set_id: str
) -> None:
    """A request past `expires_at` cannot be approved."""
    past = datetime.now(UTC) - timedelta(seconds=1)
    request = runner.run(
        service.upsert_request(change_set_id, RiskTier.LOW, 1, REQUESTER, past)
    )

    with pytest.raises(ApprovalClosedError, match="expired"):
        runner.run(service.decide(request.id, "bob", Decision.APPROVE, "api", ""))


def test_an_unknown_request_is_named(
    runner: asyncio.Runner, service: ApprovalService, change_set_id: str
) -> None:
    """A decision on a request that does not exist raises a typed error."""
    del change_set_id
    with pytest.raises(ApprovalRequestNotFoundError, match="01JNOSUCHREQUEST"):
        runner.run(
            service.decide(
                "01JNOSUCHREQUEST0000000000", "bob", Decision.APPROVE, "api", ""
            )
        )


def test_each_decision_writes_one_audit_event(
    runner: asyncio.Runner,
    service: ApprovalService,
    change_set_id: str,
    engine: Engine,
) -> None:
    """The audit row names the actor, the surface, and the state change."""
    request = open_request(runner, service, change_set_id)
    runner.run(service.decide(request.id, "bob", Decision.APPROVE, "cli", "ok"))

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT actor, actor_kind, surface, action, object_type, object_id, "
                "before, after FROM hco.audit_event"
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        (
            "bob",
            "user",
            "cli",
            "approval.decide",
            "approval_request",
            request.id,
            {"state": "pending"},
            {"state": "approved", "decision": "approve", "approvals": 1},
        )
    ]
