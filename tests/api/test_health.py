"""The health route, the request id, and the problem documents of the API."""

import asyncio
from http import HTTPStatus

from pydantic import SecretStr

from hco_core import __version__
from hco_core.api.app import create_app
from hco_core.api.deps import ApiDeps
from hco_core.api.errors import PROBLEM_JSON
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.settings import CoreSettings
from tests.api.conftest import Api

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


def test_health_is_503_when_the_database_is_unreachable(
    api: Api, runner: asyncio.Runner
) -> None:
    """An unreachable database gives a 503 problem that shows no URL part."""
    settings = CoreSettings(database_url=SecretStr(UNREACHABLE_URL))
    engine = create_database_engine(settings)
    deps = ApiDeps(
        engine=engine, sessions=create_sessionmaker(engine), review=api.deps.review
    )
    down = Api(runner, create_app(settings, deps), deps, api.root)

    response = down.call("GET", "/health")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["type"] == "urn:hco-core:problem:database-unavailable"
    assert "s3cret" not in response.text
    assert "127.0.0.1" not in response.text
    runner.run(engine.dispose())


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
