"""The ledger delivers each notification once per run, channel, and purpose (AC-3)."""

import asyncio
from typing import override

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from hco_core.domain.notify import DeliveryHandle, Notification
from hco_core.ports import NotificationChannel
from hco_core.services.audit import Actor
from hco_core.services.ledger import NotificationLedger, UnknownChannelError
from hco_core.services.runs import RunRepository

MESSAGE = Notification(title="Approval needed", body="r1 waits for you.")


class RecordingChannel(NotificationChannel):
    """A channel that records each call and the ledger rows at send time."""

    def __init__(self, engine: Engine, *, fail: bool = False) -> None:
        self.engine = engine
        self.fail = fail
        self.sent: list[Notification] = []
        self.updated: list[DeliveryHandle] = []
        self.states_at_send: list[str] = []

    @override
    async def send(self, message: Notification) -> DeliveryHandle:
        """Record the message and the ledger state that `send` sees.

        Raises:
            ConnectionError: If the channel was made to fail.

        """
        await asyncio.sleep(0)
        with self.engine.connect() as connection:
            self.states_at_send.extend(
                connection.scalars(text("SELECT state FROM hco.notification_delivery"))
            )
        self.sent.append(message)
        if self.fail:
            message_text = "channel is down"
            raise ConnectionError(message_text)
        return DeliveryHandle(channel="recording", external_id=f"m{len(self.sent)}")

    @override
    async def update(self, handle: DeliveryHandle, message: Notification) -> None:
        """Record the handle."""
        await asyncio.sleep(0)
        self.updated.append(handle)


@pytest.fixture
def run_id(
    runner: asyncio.Runner, sessions: async_sessionmaker[AsyncSession], device_id: str
) -> str:
    """Create one run."""
    runs = RunRepository(sessions)
    actor = Actor("alice", "user", "api")
    change_set_id = runner.run(runs.create_change_set("change", actor))
    return runner.run(runs.create_run(change_set_id, device_id, "thread-1", actor))


def ledger_rows(engine: Engine) -> list[tuple[object, ...]]:
    """Return the purpose, state, handle, and send flag of every ledger row."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT purpose, state, handle, sent_at IS NOT NULL "
                "FROM hco.notification_delivery ORDER BY created_at, purpose"
            )
        ).all()
    return [tuple(row) for row in rows]


def test_the_row_exists_before_send_and_holds_the_handle_after(
    runner: asyncio.Runner,
    sessions: async_sessionmaker[AsyncSession],
    engine: Engine,
    run_id: str,
) -> None:
    """`send` sees a pending row, and the row then holds the handle."""
    channel = RecordingChannel(engine)
    ledger = NotificationLedger(sessions, {"recording": channel})

    handle = runner.run(ledger.deliver(run_id, "recording", "pending", MESSAGE))

    assert handle == DeliveryHandle(channel="recording", external_id="m1")
    assert channel.states_at_send == ["pending"]
    assert ledger_rows(engine) == [
        (
            "pending",
            "sent",
            {"channel": "recording", "external_id": "m1", "metadata": {}},
            True,
        )
    ]


def test_a_second_call_updates_and_does_not_send_again(
    runner: asyncio.Runner,
    sessions: async_sessionmaker[AsyncSession],
    engine: Engine,
    run_id: str,
) -> None:
    """A node that runs again rewrites the message in place."""
    channel = RecordingChannel(engine)
    ledger = NotificationLedger(sessions, {"recording": channel})

    first = runner.run(ledger.deliver(run_id, "recording", "pending", MESSAGE))
    second = runner.run(ledger.deliver(run_id, "recording", "pending", MESSAGE))

    assert second == first
    assert channel.sent == [MESSAGE]
    assert channel.updated == [first]
    assert len(ledger_rows(engine)) == 1


def test_another_purpose_is_another_delivery(
    runner: asyncio.Runner,
    sessions: async_sessionmaker[AsyncSession],
    engine: Engine,
    run_id: str,
) -> None:
    """The key is the run, the channel, and the purpose together."""
    channel = RecordingChannel(engine)
    ledger = NotificationLedger(sessions, {"recording": channel})

    runner.run(ledger.deliver(run_id, "recording", "pending", MESSAGE))
    runner.run(ledger.deliver(run_id, "recording", "decided", MESSAGE))

    assert channel.sent == [MESSAGE, MESSAGE]
    assert [row[0] for row in ledger_rows(engine)] == ["pending", "decided"]


def test_a_failed_send_is_recorded_and_never_sent_again(
    runner: asyncio.Runner,
    sessions: async_sessionmaker[AsyncSession],
    engine: Engine,
    run_id: str,
) -> None:
    """The ledger gives at most one send. A failure stays in the ledger."""
    channel = RecordingChannel(engine, fail=True)
    ledger = NotificationLedger(sessions, {"recording": channel})

    with pytest.raises(ConnectionError):
        runner.run(ledger.deliver(run_id, "recording", "pending", MESSAGE))
    with capture_logs() as lines:
        assert (
            runner.run(ledger.deliver(run_id, "recording", "pending", MESSAGE)) is None
        )

    assert len(channel.sent) == 1
    assert channel.updated == []
    assert ledger_rows(engine) == [("pending", "failed", None, False)]
    assert lines == [not_sent_warning(run_id, "failed")]


def not_sent_warning(run_id: str, state: str) -> dict[str, object]:
    """Return the log line of a notification that the ledger does not send."""
    return {
        "event": "notification.not_sent",
        "log_level": "warning",
        "run_id": run_id,
        "channel": "recording",
        "purpose": "pending",
        "state": state,
    }


def test_a_pending_row_without_a_handle_is_not_sent_and_is_logged(
    runner: asyncio.Runner,
    sessions: async_sessionmaker[AsyncSession],
    engine: Engine,
    run_id: str,
) -> None:
    """A send that stopped before it returned is not sent again, and a warning says so."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO hco.notification_delivery "
                "(id, run_id, channel, purpose, state) "
                "VALUES ('01JLEDGERROW00000000000000', :run_id, 'recording', "
                "'pending', 'pending')"
            ),
            {"run_id": run_id},
        )
    channel = RecordingChannel(engine)
    ledger = NotificationLedger(sessions, {"recording": channel})

    with capture_logs() as lines:
        assert (
            runner.run(ledger.deliver(run_id, "recording", "pending", MESSAGE)) is None
        )

    assert channel.sent == []
    assert channel.updated == []
    assert lines == [not_sent_warning(run_id, "pending")]


def test_concurrent_deliveries_send_once(
    runner: asyncio.Runner,
    sessions: async_sessionmaker[AsyncSession],
    engine: Engine,
    run_id: str,
) -> None:
    """Five calls at the same time give one send and one row; a later call updates."""
    channel = RecordingChannel(engine)
    ledger = NotificationLedger(sessions, {"recording": channel})

    async def deliver_five() -> list[DeliveryHandle | None]:
        return await asyncio.gather(
            *(ledger.deliver(run_id, "recording", "pending", MESSAGE) for _ in range(5))
        )

    runner.run(deliver_five())
    updates_before = len(channel.updated)
    runner.run(ledger.deliver(run_id, "recording", "pending", MESSAGE))

    assert channel.sent == [MESSAGE]
    assert len(ledger_rows(engine)) == 1
    assert len(channel.updated) == updates_before + 1


def test_an_unknown_channel_is_named(
    runner: asyncio.Runner,
    sessions: async_sessionmaker[AsyncSession],
    engine: Engine,
    run_id: str,
) -> None:
    """The error lists the configured channels and writes no row."""
    ledger = NotificationLedger(sessions, {"recording": RecordingChannel(engine)})

    with pytest.raises(UnknownChannelError, match=r"'slack'.*recording"):
        runner.run(ledger.deliver(run_id, "slack", "pending", MESSAGE))
    assert ledger_rows(engine) == []
