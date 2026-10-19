"""Write `audit_event` rows in the transaction of the change they record."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from hco_core.db.models import AuditEvent, JSONObject
from hco_core.domain.approval import ActorKind, Surface


@dataclass(frozen=True, slots=True)
class Actor:
    """Who did something, on which surface, and in which request.

    `request_id` is `None` only when no HTTP request started the action.
    """

    name: str
    kind: ActorKind
    surface: Surface
    request_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEntry:
    """One action on one object, with the state before and after it."""

    actor: Actor
    action: str
    object_type: str
    object_id: str
    before: JSONObject | None = None
    after: JSONObject | None = None


def add_audit_event(session: AsyncSession, entry: AuditEntry) -> None:
    """Add the entry to the session. It commits with the session."""
    session.add(
        AuditEvent(
            actor=entry.actor.name,
            actor_kind=entry.actor.kind,
            surface=entry.actor.surface,
            action=entry.action,
            object_type=entry.object_type,
            object_id=entry.object_id,
            before=entry.before,
            after=entry.after,
            request_id=entry.actor.request_id,
        )
    )
