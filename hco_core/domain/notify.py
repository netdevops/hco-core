"""Notifications and change proposals."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from hco_core.domain.base import BaseModel


class ProposalStatus(StrEnum):
    """The state of a change proposal on an external review system."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class Notification(BaseModel):
    """A message about a run."""

    title: str
    body: str
    severity: str = "info"
    run_id: str | None = None
    actions: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = {}


class DeliveryHandle(BaseModel):
    """A reference to a delivered message, so it can be rewritten in place.

    Updating is required rather than optional. An approval card still showing
    Approve and Reject buttons after the run was decided elsewhere is worse than
    no card: someone will click it.
    """

    channel: str
    external_id: str
    metadata: Mapping[str, Any] = {}


class ChangeProposal(BaseModel):
    """A change offered for review on an external system, such as a pull request."""

    title: str
    body: str
    branch: str
    path_contents: Mapping[str, str] = {}
    labels: tuple[str, ...] = ()


class ProposalHandle(BaseModel):
    """A reference to an open proposal."""

    provider: str
    external_id: str
    url: str = ""
