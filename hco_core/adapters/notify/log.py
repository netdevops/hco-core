"""A notification channel that writes structlog lines."""

import structlog
from ulid import ULID

from hco_core.domain.notify import DeliveryHandle, Notification


class LogChannel:
    """Write each notification to the log. For development and tests.

    The line holds `title` and `body` as they are, and never `metadata`. A
    caller must not put device output or configuration text in `title` or
    `body`, because all device output is untrusted and a log must not hold it.
    """

    def __init__(self) -> None:
        self._log = structlog.get_logger(__name__)

    async def send(self, message: Notification) -> DeliveryHandle:
        """Log the message and return a handle with a new id."""
        handle = DeliveryHandle(channel="log", external_id=str(ULID()))
        await self._log.ainfo("notification.sent", **_fields(handle, message))
        return handle

    async def update(self, handle: DeliveryHandle, message: Notification) -> None:
        """Log the new text of the message under the same id."""
        await self._log.ainfo("notification.updated", **_fields(handle, message))


def _fields(handle: DeliveryHandle, message: Notification) -> dict[str, object]:
    return {
        "external_id": handle.external_id,
        "run_id": message.run_id,
        "severity": message.severity,
        "title": message.title,
        "body": message.body,
        "actions": list(message.actions),
    }
