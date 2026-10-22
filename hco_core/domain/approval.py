"""Run states, approval requests, and the decisions of approvers."""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime

from hco_core.domain.base import BaseModel

type ActorKind = Literal["user", "agent", "system"]
type Surface = Literal["ui", "pr", "chat", "api", "cli", "mcp"]


class RunState(StrEnum):
    """The states of a remediation run, as docs/spec/03 defines them."""

    DRAFT = "DRAFT"
    COLLECTING = "COLLECTING"
    REMEDIATING = "REMEDIATING"
    ASSESSING = "ASSESSING"
    EVALUATING_POLICY = "EVALUATING_POLICY"
    BLOCKED = "BLOCKED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    APPLYING = "APPLYING"
    VERIFYING = "VERIFYING"
    COMMITTED = "COMMITTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class ApprovalState(StrEnum):
    """The state of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Decision(StrEnum):
    """What an approver decided."""

    APPROVE = "approve"
    REJECT = "reject"


class ApprovalRequest(BaseModel):
    """The one approval that a change set waits for."""

    id: str
    change_set_id: str
    state: ApprovalState
    required_count: int
    requester: str
    expires_at: AwareDatetime
    interrupt_key: str


class ApprovalDecision(BaseModel):
    """One person's decision on an approval request."""

    id: str
    request_id: str
    actor: str
    actor_kind: ActorKind
    decision: Decision
    surface: Surface
    comment: str
