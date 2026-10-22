"""Structured logging of hco-core and of the processes built on it."""

import logging
import sys
from typing import TYPE_CHECKING

import structlog

from hco_core.settings import CoreSettings

if TYPE_CHECKING:
    from structlog.typing import Processor

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(settings: CoreSettings) -> None:
    """Send structlog and standard-library log lines to stdout in one format.

    The uvicorn loggers lose their own handlers, so their lines also carry the
    context variables, for example the request id. A JSON traceback holds no
    local variables, because they can hold a secret.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    renderers: list[Processor] = (
        [
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            ),
            structlog.processors.JSONRenderer(),
        ]
        if settings.log_format == "json"
        else [structlog.dev.ConsoleRenderer()]
    )
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                *renderers,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
