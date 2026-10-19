"""Deliver notifications through the `notification_delivery` ledger.

A graph node can run again after a resume. The ledger row of each run,
channel, and purpose makes sure that a message goes out at most once.
"""

from collections.abc import Mapping

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.db.models import NotificationDelivery
from hco_core.db.types import new_id
from hco_core.domain.notify import DeliveryHandle, Notification
from hco_core.ports import NotificationChannel


class UnknownChannelError(LookupError):
    """No configured channel has the name."""


class NotificationLedger:
    """Send each notification once, and rewrite it in place after that."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        channels: Mapping[str, NotificationChannel],
    ) -> None:
        self._sessions = sessions
        self._channels = channels

    async def deliver(
        self, run_id: str, channel: str, purpose: str, message: Notification
    ) -> DeliveryHandle | None:
        """Send the message on the first call, and update it on a later call.

        The ledger row commits before `send()`. A row without a handle means an
        earlier send failed or did not finish, so nothing goes out again and
        the return is `None`.

        Raises:
            UnknownChannelError: If no configured channel has the name.

        """
        if channel not in self._channels:
            known = ", ".join(sorted(self._channels))
            message_text = f"unknown channel {channel!r}; configured: {known}"
            raise UnknownChannelError(message_text)
        target = self._channels[channel]
        async with self._sessions.begin() as session:
            claimed = await session.scalar(
                insert(NotificationDelivery)
                .values(
                    id=new_id(),
                    run_id=run_id,
                    channel=channel,
                    purpose=purpose,
                    state="pending",
                )
                .on_conflict_do_nothing(index_elements=["run_id", "channel", "purpose"])
                .returning(NotificationDelivery.id)
            )
            existing = (
                await session.execute(
                    select(NotificationDelivery.id, NotificationDelivery.handle).where(
                        NotificationDelivery.run_id == run_id,
                        NotificationDelivery.channel == channel,
                        NotificationDelivery.purpose == purpose,
                    )
                )
            ).one()
        if claimed is not None:
            return await self._send(target, claimed, message)
        if existing.handle is None:
            return None
        handle = DeliveryHandle.model_validate(existing.handle)
        await target.update(handle, message)
        await self._record(existing.id, state="sent")
        return handle

    async def _send(
        self, target: NotificationChannel, row_id: str, message: Notification
    ) -> DeliveryHandle:
        try:
            handle = await target.send(message)
        except Exception:
            await self._record(row_id, state="failed")
            raise
        await self._record(
            row_id,
            state="sent",
            handle=handle.model_dump(mode="json"),
            sent_at=func.now(),
        )
        return handle

    async def _record(self, row_id: str, **values: object) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id == row_id)
                .values(updated_at=func.now(), **values)
            )
