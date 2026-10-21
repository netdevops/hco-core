"""The run routes: create a run that parks for approval, list, show, and diff."""

from http import HTTPStatus

from pydantic import SecretStr
from sqlalchemy import Engine, text
from structlog.testing import capture_logs

from hco_core.api.app import create_app
from hco_core.api.errors import PROBLEM_JSON
from hco_core.api.routers.runs import MAX_ERROR_CHARACTERS
from hco_core.domain.approval import RunState
from hco_core.services.audit import Actor
from hco_core.services.runs import RunUpdate
from hco_core.settings import CoreSettings
from tests.api.conftest import ACTOR, Api, write_inventory
from tests.api.test_health import UNREACHABLE_URL

SYSTEM = Actor("test", "system", "api")


def create_run(api: Api, device_id: str = "r1") -> str:
    """Sync the devices, create a run on the device, and return the run id."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    response = api.call(
        "POST", "/api/v1/runs", actor=ACTOR, json={"device_id": device_id}
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    return response.json()["run_id"]


def draft_run(api: Api) -> str:
    """Create a run row that no graph started, and return its id."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    device_id = api.call("GET", "/api/v1/devices/r1").json()["id"]
    runs = api.deps.review.runs
    change_set_id = api.runner.run(runs.create_change_set("draft", SYSTEM))
    return api.runner.run(runs.create_run(change_set_id, device_id, "thread-x", SYSTEM))


def test_create_returns_202_and_the_run_parks_for_approval(api: Api) -> None:
    """With no assessment, the run reaches AWAITING_APPROVAL at HIGH (AC-10)."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    created = api.call("POST", "/api/v1/runs", actor=ACTOR, json={"device_id": "r1"})
    run_id = created.json()["run_id"]
    shown = api.call("GET", f"/api/v1/runs/{run_id}").json()

    assert created.status_code == HTTPStatus.ACCEPTED
    assert created.headers["location"] == f"/api/v1/runs/{run_id}"
    assert created.json()["state"] == RunState.DRAFT
    assert shown["state"] == RunState.AWAITING_APPROVAL
    assert shown["risk_tier_final"] == "HIGH"
    assert shown["requested_by"] == ACTOR
    assert shown["sot_device_id"] == "r1"
    assert shown["errors"] == []


def test_the_requester_is_on_the_change_set_and_the_request(
    api: Api, engine: Engine
) -> None:
    """The actor is `change_set.requested_by` and the approval requester."""
    create_run(api)

    with engine.connect() as connection:
        requested_by = connection.execute(
            text("SELECT requested_by FROM hco.change_set")
        ).scalar_one()
        requester = connection.execute(
            text("SELECT requester FROM hco.approval_request")
        ).scalar_one()
    assert (requested_by, requester) == (ACTOR, ACTOR)


def test_create_without_the_actor_header_is_401_and_writes_nothing(
    api: Api, engine: Engine
) -> None:
    """A run needs a requester."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    response = api.call("POST", "/api/v1/runs", json={"device_id": "r1"})

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_JSON
    assert "X-HCO-Actor" in response.json()["detail"]
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM hco.change_set")).scalar_one()
            == 0
        )


def test_create_on_an_unknown_device_is_404_naming_the_device(api: Api) -> None:
    """An empty inventory has no device to run on."""
    write_inventory(api.root, "devices: []\n")
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    response = api.call(
        "POST", "/api/v1/runs", actor=ACTOR, json={"device_id": "missing"}
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "'missing'" in response.json()["detail"]


def test_a_device_with_no_intended_config_ends_blocked_with_its_error(api: Api) -> None:
    """The run shows the error that the graph recorded."""
    run_id = create_run(api, "r2")

    shown = api.call("GET", f"/api/v1/runs/{run_id}").json()

    assert shown["state"] == RunState.BLOCKED
    assert "no intended config" in shown["errors"][0]


def test_an_error_shows_no_credential_of_a_url_and_is_cut(api: Api) -> None:
    """A URL loses its user and password, and a long error is cut."""
    run_id = draft_run(api)
    long_error = "postgresql://hco:s3cret@db.example/hco failed " + "x" * 2000
    api.runner.run(
        api.deps.review.runs.update_run(
            run_id, RunUpdate(error={"errors": [long_error]}), SYSTEM
        )
    )

    errors = api.call("GET", f"/api/v1/runs/{run_id}").json()["errors"]

    assert "s3cret" not in errors[0]
    assert errors[0].startswith("postgresql://***@db.example/hco failed")
    assert len(errors[0]) == MAX_ERROR_CHARACTERS


def test_diff_renders_the_remediation_the_rollback_and_the_future(api: Api) -> None:
    """The engine renders each stored dump as config lines."""
    run_id = create_run(api)

    response = api.call("GET", f"/api/v1/runs/{run_id}/diff")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["remediation"] == ["ntp server 192.0.2.10"]
    assert response.json()["rollback"] == ["no ntp server 192.0.2.10"]
    assert "ntp server 192.0.2.10" in response.json()["future"]
    assert "hostname r1" in response.json()["future"]


def test_diff_of_a_run_with_no_remediation_is_409(api: Api) -> None:
    """A run that has no future snapshot yet has no diff."""
    run_id = draft_run(api)

    response = api.call("GET", f"/api/v1/runs/{run_id}/diff")

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["type"] == "urn:hco-core:problem:run-not-ready"
    assert "DRAFT" in response.json()["detail"]


def test_an_unknown_run_is_404(api: Api) -> None:
    """Show and diff name the run id that is not there."""
    for path in ("/api/v1/runs/missing", "/api/v1/runs/missing/diff"):
        response = api.call("GET", path)

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.json()["type"] == "urn:hco-core:problem:run-not-found"


def test_the_list_pages_the_newest_run_first_and_filters_by_state(api: Api) -> None:
    """Each page holds at most `limit` runs and a cursor to the next page."""
    parked = create_run(api)
    blocked = create_run(api, "r2")

    first = api.call("GET", "/api/v1/runs?limit=1").json()
    second = api.call(
        "GET", f"/api/v1/runs?limit=1&cursor={first['next_cursor']}"
    ).json()
    only_blocked = api.call("GET", "/api/v1/runs?state=BLOCKED").json()

    assert [item["id"] for item in first["items"]] == [blocked]
    assert [item["id"] for item in second["items"]] == [parked]
    assert second["next_cursor"] is None
    assert [item["id"] for item in only_blocked["items"]] == [blocked]


def test_a_graph_that_fails_to_start_logs_the_run_and_no_detail(api: Api) -> None:
    """The run stays in DRAFT, and the log names the run and the error class."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    broken = create_app(CoreSettings(database_url=SecretStr(UNREACHABLE_URL)), api.deps)
    api.app = broken

    with capture_logs() as lines:
        response = api.call(
            "POST", "/api/v1/runs", actor=ACTOR, json={"device_id": "r1"}
        )

    run_id = response.json()["run_id"]
    failed = [line for line in lines if line["event"] == "run_failed"]
    assert response.status_code == HTTPStatus.ACCEPTED
    assert failed == [
        {
            "event": "run_failed",
            "log_level": "error",
            "run_id": run_id,
            "error": "OperationalError",
        }
    ]
    assert api.call("GET", f"/api/v1/runs/{run_id}").json()["state"] == RunState.DRAFT
