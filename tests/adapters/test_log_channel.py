"""The log channel writes each notification as a structlog line."""

import asyncio

from structlog.testing import capture_logs

from hco_core.adapters.notify.log import LogChannel
from hco_core.domain.notify import Notification
from hco_core.ports import NotificationChannel

MESSAGE = Notification(title="Approval needed", body="r1 waits.", run_id="run-1")


def test_the_log_channel_satisfies_the_port() -> None:
    """`LogChannel` is a `NotificationChannel`."""
    assert isinstance(LogChannel(), NotificationChannel)


def test_send_and_update_write_one_line_each() -> None:
    """Each call logs the title, the run, and the handle id."""
    channel = LogChannel()
    with capture_logs() as lines:
        handle = asyncio.run(channel.send(MESSAGE))
        asyncio.run(channel.update(handle, MESSAGE))

    assert handle.channel == "log"
    assert [line["event"] for line in lines] == [
        "notification.sent",
        "notification.updated",
    ]
    assert all(line["external_id"] == handle.external_id for line in lines)
    assert all(line["title"] == MESSAGE.title for line in lines)
    assert all(line["run_id"] == "run-1" for line in lines)
