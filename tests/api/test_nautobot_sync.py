"""A device sync through the API reads the Nautobot source of truth."""

import json
from http import HTTPStatus

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from hco_core.adapters.sot.nautobot import NautobotSourceOfTruth
from hco_core.ports import SourceOfTruth
from tests.adapters.test_nautobot import FakeNautobot
from tests.api.conftest import ACTOR, Api

TOKEN = SecretStr("sync-token")


class NulNameNautobot(FakeNautobot):
    """Serve the device page with a NUL in the name of the first device."""

    def __init__(self) -> None:
        """Change the name of the first device."""
        super().__init__()
        self.devices[0]["name"] = "edge\x00one"
        self.refuse = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Refuse the token when `refuse` is set."""
        if self.refuse:
            return httpx.Response(401, content=json.dumps({"detail": "no"}))
        return super().__call__(request)


@pytest.fixture
def fake() -> NulNameNautobot:
    """Give the fake Nautobot of the test."""
    return NulNameNautobot()


@pytest.fixture
def sot(fake: NulNameNautobot) -> SourceOfTruth:
    """Give the Nautobot source of truth over the fake Nautobot."""
    return NautobotSourceOfTruth(
        "https://nautobot.example", TOKEN, transport=httpx.MockTransport(fake)
    )


def test_a_device_with_a_nul_name_does_not_stop_the_sync(api: Api) -> None:
    """The sync stores every other device, and no name holds a NUL."""
    response = api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"synced": 2}
    names = [
        item["name"] for item in api.call("GET", "/api/v1/devices").json()["items"]
    ]
    assert sorted(names) == ["core-1", "edge-2"]


def test_a_refused_token_logs_a_fixed_reason(api: Api, fake: NulNameNautobot) -> None:
    """A 401 gives a 503, and the log line gives the reason EACCES."""
    fake.refuse = True

    with capture_logs() as lines:
        response = api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert {
        "event": "request_failed",
        "log_level": "error",
        "error": "SourceOfTruthUnavailableError",
        "cause": "SourceOfTruthUnreachableError",
        "reason": "EACCES",
    } in lines
