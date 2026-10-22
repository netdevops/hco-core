"""The health route, the request id, and the problem documents of the API."""

import asyncio
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import structlog
from pydantic import SecretStr
from sqlalchemy import make_url
from sqlalchemy.exc import DBAPIError
from structlog.testing import capture_logs

from hco_core import __version__
from hco_core.api.app import RequestIdMiddleware, app_from_env, create_app
from hco_core.api.deps import (
    ApiDeps,
    MissingValidationKeyError,
    build_deps,
    resume_not_wired,
)
from hco_core.api.errors import PROBLEM_JSON, failure_reason
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.settings import CoreSettings
from tests.api.conftest import Api

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

    from hco_core.domain.approval import ApprovalRequest

UNREACHABLE_URL = "postgresql://hco:s3cret@127.0.0.1:1/hco"


def test_health_reports_the_database_and_the_version(api: Api) -> None:
    """A reachable database gives 200 with the package version."""
    response = api.call("GET", "/health")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "version": __version__,
    }


DATABASE_DETAIL = (
    "the database does not answer; check HCO_DATABASE_URL, "
    "and read the server log for the cause"
)


@pytest.mark.parametrize(
    ("password", "port", "reason"),
    [
        ("s3cret", 1, "connection refused"),
        ("s3cret", None, "password authentication failed (28P01)"),
    ],
    ids=["closed-port", "wrong-password"],
)
def test_health_is_503_naming_the_setting_and_logging_a_safe_cause(  # ruff: ignore[too-many-arguments, too-many-positional-arguments]
    api: Api,
    runner: asyncio.Runner,
    database_url: str,
    password: str,
    port: int | None,
    reason: str,
) -> None:
    """The detail names the setting, and the log names the cause with no URL part."""
    url = make_url(database_url).set(password=password)
    url = url if port is None else url.set(port=port)
    settings = CoreSettings(
        database_url=SecretStr(url.render_as_string(hide_password=False))
    )
    engine = create_database_engine(settings)
    deps = ApiDeps(
        engine=engine, sessions=create_sessionmaker(engine), review=api.deps.review
    )
    down = Api(runner, create_app(settings, deps), deps, api.root)

    with capture_logs() as lines:
        response = down.call("GET", "/health")

    runner.run(engine.dispose())
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["type"] == "urn:hco-core:problem:database-unavailable"
    assert response.json()["detail"] == DATABASE_DETAIL
    failed = [line for line in lines if line["event"] == "request_failed"]
    assert failed == [
        {
            "event": "request_failed",
            "log_level": "error",
            "error": "DatabaseUnavailableError",
            "cause": "OperationalError",
            "reason": reason,
        }
    ]
    for leak in (password, str(url.host), str(url.port)):
        assert leak not in response.text
        assert leak not in repr(lines)


def test_an_unknown_route_is_a_problem_document(api: Api) -> None:
    """An HTTP error of the framework is also a problem document."""
    response = api.call("GET", "/api/v1/nothing")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json() == {
        "type": "urn:hco-core:problem:http-404",
        "title": "Not Found",
        "status": 404,
        "detail": "Not Found",
    }


def test_the_factory_builds_the_app_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    runner: asyncio.Runner,
    restore_logging: None,
    tmp_path: Path,
) -> None:
    """`app_from_env` reads the `HCO_` variables, and the stop disposes of the engine."""
    del restore_logging
    monkeypatch.setenv("HCO_VALIDATION_KEY", "k" * 32)
    monkeypatch.setenv("HCO_SOT_PATH", str(tmp_path / "inventory.yaml"))

    application = app_from_env()

    async def start_and_stop() -> None:
        async with application.router.lifespan_context(application):
            pass

    runner.run(start_and_stop())
    assert application.title == "hco-core"


def test_the_dependencies_need_the_validation_key() -> None:
    """The mock connection verifies plan tokens with the key, so it is required."""
    with pytest.raises(MissingValidationKeyError, match="HCO_VALIDATION_KEY"):
        build_deps(CoreSettings(validation_key=None))


def test_no_route_resumes_a_run_before_the_approvals_route(
    runner: asyncio.Runner,
) -> None:
    """The approval service of the API refuses to resume until #36 wires it."""
    with pytest.raises(NotImplementedError, match="#36"):
        runner.run(resume_not_wired(cast("ApprovalRequest", None), None))


def test_the_request_id_middleware_passes_a_lifespan_scope_through(
    runner: asyncio.Runner,
) -> None:
    """Only an HTTP request gets a request id."""
    seen: list[str] = []

    async def inner(scope: "Scope", receive: "Receive", send: "Send") -> None:
        del receive, send
        await asyncio.sleep(0)
        seen.append(scope["type"])
        seen.extend(structlog.contextvars.get_contextvars())

    middleware = RequestIdMiddleware(inner)
    runner.run(
        middleware({"type": "lifespan"}, cast("Receive", None), cast("Send", None))
    )

    assert seen == ["lifespan"]


def test_a_405_keeps_the_allow_header(api: Api) -> None:
    """The problem document keeps the headers of the framework error."""
    response = api.call("PUT", "/health")

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert response.headers["allow"] == "GET"


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_the_api_serves_no_html_documentation(api: Api, path: str) -> None:
    """The schema is at `/openapi.json`. No page loads a script from a CDN."""
    assert api.call("GET", path).status_code == HTTPStatus.NOT_FOUND
    assert api.call("GET", "/openapi.json").status_code == HTTPStatus.OK


def test_each_response_says_nosniff(api: Api) -> None:
    """A browser must not guess a content type from a device field."""
    for path in ("/health", "/api/v1/nothing"):
        assert api.call("GET", path).headers["x-content-type-options"] == "nosniff"


class StatementError(Exception):
    """A database error with a SQLSTATE, as psycopg raises after a connect."""

    sqlstate = "42P01"


@pytest.mark.parametrize(
    ("cause", "reason"),
    [
        (TimeoutError(), "timeout"),
        (OSError("no errno"), "unknown"),
        (DBAPIError("SELECT 1", None, StatementError("no table")), "SQLSTATE 42P01"),
        (DBAPIError("SELECT 1", None, Exception("host secret.example")), "unknown"),
        (ValueError("text"), "unknown"),
    ],
    ids=["timeout", "os-without-errno", "sqlstate", "unknown-database", "other"],
)
def test_the_failure_reason_is_a_fixed_text(cause: BaseException, reason: str) -> None:
    """The reason never repeats the text of the error."""
    assert failure_reason(cause) == reason
