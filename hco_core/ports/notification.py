"""Delivering messages to humans."""

from typing import Protocol, runtime_checkable

from hco_core.domain.notify import DeliveryHandle, Notification


@runtime_checkable
class NotificationChannel(Protocol):
    """Somewhere a human will see a message."""

    async def send(self, message: Notification) -> DeliveryHandle:
        """Deliver a message."""
        ...

    async def update(self, handle: DeliveryHandle, message: Notification) -> None:
        """Rewrite a delivered message in place.

        Required, not optional. An approval card still showing Approve and
        Reject buttons after the run was decided elsewhere is worse than no
        card: someone will click it.
        """
        ...
