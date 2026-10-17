"""The structlog setup sends every log line to stdout in one format."""

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from hco_core.logging import UVICORN_LOGGERS, configure_logging
from hco_core.settings import CoreSettings


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Put back the root handlers and the structlog defaults after each test."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    root.handlers = handlers
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


pytestmark = pytest.mark.usefixtures("restore_logging")


def test_json_lines_merge_the_context(capsys: pytest.CaptureFixture[str]) -> None:
    """Structlog and standard-library lines carry the bound context."""
    configure_logging(CoreSettings(log_format="json"))
    structlog.contextvars.bind_contextvars(request_id="r-1")

    structlog.get_logger().info("structlog probe")
    logging.getLogger("uvicorn.error").info("stdlib probe")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == ["structlog probe", "stdlib probe"]
    assert all(line["request_id"] == "r-1" for line in lines)
    assert all(line["level"] == "info" for line in lines)


def test_console_format_selects_the_console_renderer() -> None:
    """The console format renders for a person, at the set level."""
    configure_logging(CoreSettings(log_format="console", log_level="WARNING"))

    root = logging.getLogger()
    formatter = root.handlers[0].formatter
    assert isinstance(formatter, structlog.stdlib.ProcessorFormatter)
    assert isinstance(formatter.processors[-1], structlog.dev.ConsoleRenderer)
    assert root.level == logging.WARNING


def test_uvicorn_loggers_lose_their_handlers_and_propagate() -> None:
    """The uvicorn lines go through the root handler, with the context."""
    logging.getLogger("uvicorn.access").addHandler(logging.NullHandler())

    configure_logging(CoreSettings())

    for name in UVICORN_LOGGERS:
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate


def divide_with_secret_local() -> float:
    """Raise ZeroDivisionError from a frame that holds a secret local variable."""
    marker = "secret-marker"
    return len(marker) / 0


def test_json_error_line_has_a_traceback_with_no_locals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A JSON traceback holds no local variables, because they can hold a secret."""
    configure_logging(CoreSettings(log_format="json"))

    try:
        divide_with_secret_local()
    except ZeroDivisionError:
        logging.getLogger("hco_core.test").exception("division failed")

    raw_line = capsys.readouterr().out.splitlines()[-1]
    exception = json.loads(raw_line)["exception"][0]
    assert exception["exc_type"] == "ZeroDivisionError"
    assert all("locals" not in frame for frame in exception["frames"])
    assert "secret-marker" not in raw_line
