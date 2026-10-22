"""Map errors to RFC 9457 problem documents.

A problem document never shows a config text, a `config_context` or
`metadata` value, or a database URL. Engine errors and unknown errors get a
fixed detail, because their text can hold config lines.
"""

import errno
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from starlette.exceptions import HTTPException

from hco_core.adapters.errors import DeviceNotFoundError, InvalidInventoryError
from hco_core.engine.errors import (
    DriverMismatchError,
    EngineError,
    InvalidConfigError,
    UnknownPlatformError,
)
from hco_core.services.approval import InvalidActorError
from hco_core.services.runs import RunNotFoundError

PROBLEM_JSON: Final = "application/problem+json"
PROBLEM_TYPE: Final = "urn:hco-core:problem:"

_log = structlog.get_logger(__name__)


class MissingActorError(PermissionError):
    """A mutating request has no `X-HCO-Actor` header."""


class DatabaseUnavailableError(RuntimeError):
    """The database does not answer."""


class DeviceSyncError(RuntimeError):
    """The database refused the device rows of a sync."""


class SourceOfTruthUnavailableError(RuntimeError):
    """The source of truth cannot be read."""


class RunNotReadyError(RuntimeError):
    """The run has no remediation yet."""


@dataclass(frozen=True, slots=True)
class Problem:
    """The status and the stable code of one error class.

    A `detail` of `None` shows the text of the error.
    """

    status: int
    code: str
    title: str
    detail: str | None = None


PROBLEMS: Final[dict[type[Exception], Problem]] = {
    MissingActorError: Problem(401, "missing-actor", "Actor required"),
    InvalidActorError: Problem(400, "invalid-actor", "Invalid actor"),
    DeviceNotFoundError: Problem(404, "device-not-found", "Device not found"),
    RunNotFoundError: Problem(404, "run-not-found", "Run not found"),
    RunNotReadyError: Problem(409, "run-not-ready", "Run not ready"),
    InvalidInventoryError: Problem(400, "invalid-inventory", "Invalid inventory"),
    DatabaseUnavailableError: Problem(
        503,
        "database-unavailable",
        "Database unavailable",
        "the database does not answer; check HCO_DATABASE_URL, "
        "and read the server log for the cause",
    ),
    DeviceSyncError: Problem(
        500,
        "device-sync-failed",
        "Device sync failed",
        "the database refused the devices",
    ),
    SourceOfTruthUnavailableError: Problem(
        503,
        "sot-unavailable",
        "Source of truth unavailable",
        "the source of truth is not readable; check HCO_SOT_PATH, "
        "and read the server log for the cause",
    ),
    UnknownPlatformError: Problem(
        400, "unknown-platform", "Unknown platform", "no driver has the platform name"
    ),
    InvalidConfigError: Problem(
        422, "invalid-config", "Invalid config", "a config or a dump is malformed"
    ),
    DriverMismatchError: Problem(
        409,
        "driver-mismatch",
        "Driver mismatch",
        "the running and the intended configs use different drivers",
    ),
    EngineError: Problem(
        500, "engine-error", "Engine error", "the remediation engine failed"
    ),
}
"""The problem of each error class. A subclass without an entry uses its base."""

DATABASE_REASONS: Final = (
    ("password authentication failed", "password authentication failed (28P01)"),
    ("Connection refused", "connection refused"),
    ("does not exist", "database or role does not exist"),
    ("timeout expired", "timeout"),
    ("could not translate host name", "unknown host"),
)
"""Phrases of a connection error that has no SQLSTATE, and the fixed reason to log."""

INTERNAL: Final = Problem(
    500, "internal-error", "Internal Server Error", "the server failed"
)


def problem_response(
    status: int,
    code: str,
    title: str,
    detail: str,
    *,
    headers: Mapping[str, str] | None = None,
    **extra: object,
) -> JSONResponse:
    """Return a problem document with the status, the code, and the detail."""
    return JSONResponse(
        {
            "type": f"{PROBLEM_TYPE}{code}",
            "title": title,
            "status": status,
            "detail": detail,
            **extra,
        },
        status_code=status,
        headers=headers,
        media_type=PROBLEM_JSON,
    )


@contextmanager
def database_errors() -> Generator[None]:
    """Raise `DatabaseUnavailableError` when the database connection fails.

    Raises:
        DatabaseUnavailableError: For a connection or an operational error.

    """
    try:
        yield
    except (OperationalError, InterfaceError) as error:
        raise DatabaseUnavailableError from error


def add_problem_handlers(app: FastAPI) -> None:
    """Send each known error, each HTTP error, and each other error as a problem."""
    for error_class in (*PROBLEMS, HTTPException, RequestValidationError, Exception):
        app.add_exception_handler(error_class, problem_for)


def problem_for(_: Request, error: Exception) -> JSONResponse:
    """Return the problem document of the error."""
    match error:
        case HTTPException(status_code=status, detail=detail, headers=headers):
            return problem_response(
                status, f"http-{status}", detail, detail, headers=headers
            )
        case RequestValidationError():
            return problem_response(
                422,
                "invalid-request",
                "Invalid request",
                "the request does not match the schema",
                errors=[
                    {"loc": [str(part) for part in item["loc"]], "msg": item["msg"]}
                    for item in error.errors()
                ],
            )
        case _:
            problem = _problem(error)
            if problem.status >= INTERNAL.status:
                _log.error(
                    "request_failed", error=type(error).__name__, **_cause(error)
                )
            detail = str(error) if problem.detail is None else problem.detail
            return problem_response(problem.status, problem.code, problem.title, detail)


def _cause(error: Exception) -> dict[str, str]:
    """Return the class and a fixed reason of the cause, never its text."""
    cause = error.__cause__
    if cause is None:
        return {}
    return {"cause": type(cause).__name__, "reason": failure_reason(cause)}


def failure_reason(cause: BaseException) -> str:
    """Return a fixed reason for an OS or a database error, with no path or URL."""
    match cause:
        case TimeoutError():
            return "timeout"
        case OSError(errno=int(code)) if code in errno.errorcode:
            return errno.errorcode[code]
        case DBAPIError(orig=orig):
            sqlstate = getattr(orig, "sqlstate", None)
            if isinstance(sqlstate, str):
                return f"SQLSTATE {sqlstate}"
            text = str(orig)
            return next(
                (reason for phrase, reason in DATABASE_REASONS if phrase in text),
                "unknown",
            )
        case _:
            return "unknown"


def _problem(error: Exception) -> Problem:
    return next(
        (PROBLEMS[cls] for cls in type(error).__mro__ if cls in PROBLEMS), INTERNAL
    )
