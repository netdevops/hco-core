"""The run routes: create a run that parks for approval, list, show, and diff."""

import asyncio
import time
from dataclasses import replace
from http import HTTPStatus

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text
from structlog.testing import capture_logs

from hco_core.api.app import create_app
from hco_core.api.errors import PROBLEM_JSON
from hco_core.api.routers.runs import (
    MAX_ERROR_CHARACTERS,
    MAX_ERRORS,
    START_ERROR,
    run_errors,
)
from hco_core.db.session import create_database_engine, create_sessionmaker
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
    assert shown["unknowns"] == ["no risk assessment"]
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


def test_a_graph_that_fails_to_start_ends_blocked_and_logs_the_class(
    api: Api,
) -> None:
    """The run is BLOCKED with an error that names the log line, and no detail."""
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
    shown = api.call("GET", f"/api/v1/runs/{run_id}").json()
    assert shown["state"] == RunState.BLOCKED
    assert shown["errors"] == [START_ERROR]


def test_a_run_that_cannot_be_blocked_keeps_its_state_and_logs_it(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The BLOCKED write is best effort. Its failure is logged with the class only."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    api.app = create_app(
        CoreSettings(database_url=SecretStr(UNREACHABLE_URL)), api.deps
    )

    async def refuse(*_: object, **__: object) -> bool:
        await asyncio.sleep(0)
        raise RuntimeError(URL_IN_ERROR)

    monkeypatch.setattr(api.deps.review.runs, "update_run", refuse)

    with capture_logs() as lines:
        response = api.call(
            "POST", "/api/v1/runs", actor=ACTOR, json={"device_id": "r1"}
        )

    run_id = response.json()["run_id"]
    assert {
        "event": "run_block_failed",
        "log_level": "error",
        "run_id": run_id,
        "error": "RuntimeError",
    } in lines
    assert "s3cret" not in repr(lines)


URL_IN_ERROR = "postgresql://hco:s3cret@db/hco"
HOSTILE_SECONDS = 0.05


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        ("postgresql://hco:p/ss@db/hco failed", "postgresql://***@db/hco failed"),
        ("postgresql://hco:p@ss@db/hco failed", "postgresql://***@db/hco failed"),
        ("postgresql://hco:p:ss@db/hco failed", "postgresql://***@db/hco failed"),
        ("redis://:pw@cache/0 down", "redis://***@cache/0 down"),
        (
            "from postgresql://a:x@one/hco to postgresql://b:y@two/hco",
            "from postgresql://***@one/hco to postgresql://***@two/hco",
        ),
        (
            "intended.txt is outside /srv/hco/inventory, see '/srv/hco/x.txt'",
            "intended.txt is outside .../inventory, see '.../x.txt'",
        ),
        ("(postgresql://hco:pw@db/hco)", "(postgresql://***@db/hco)"),
        ("'postgresql://hco:pw@db/hco'", "'postgresql://***@db/hco'"),
        ("x://a:b@h,y://c:d@k", "x://***@k"),
    ],
    ids=[
        "slash",
        "at",
        "colon",
        "no-user",
        "two-urls",
        "paths",
        "parentheses",
        "quotes",
        "two-in-one-token",
    ],
)
def test_a_run_error_shows_no_credential_and_no_absolute_path(
    error: str, shown: str
) -> None:
    """The user information of a URL goes, and a path keeps its base name only."""
    assert run_errors({"errors": [error]}) == (shown,)


def test_an_intended_config_outside_the_folder_shows_no_folder(api: Api) -> None:
    """The run error names the key and the device, not the server folder."""
    outside = "  - {id: r3, name: edge-3, platform_name: CISCO_IOS, mgmt_address: 10.0.0.3, intended_config: /nonexistent/dir/intended.txt}\n"
    write_inventory(api.root, (api.root / "inventory.yaml").read_text() + outside)

    errors = api.call("GET", f"/api/v1/runs/{create_run(api, 'r3')}").json()["errors"]

    assert "outside the inventory folder" in errors[0]
    assert str(api.root) not in errors[0]
    assert "/nonexistent/dir" not in errors[0]


def test_the_run_reads_are_503_when_the_database_is_down(
    api: Api, runner: asyncio.Runner
) -> None:
    """A database that does not answer gives the same problem as `/health`."""
    engine = create_database_engine(
        CoreSettings(database_url=SecretStr(UNREACHABLE_URL))
    )
    deps = replace(api.deps, engine=engine, sessions=create_sessionmaker(engine))
    down = Api(runner, create_app(CoreSettings(), deps), deps, api.root)
    run_id = "01JRUN0000000000000000000A"

    for path in (
        "/api/v1/runs",
        f"/api/v1/runs/{run_id}",
        f"/api/v1/runs/{run_id}/diff",
    ):
        response = down.call("GET", path)

        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["type"] == "urn:hco-core:problem:database-unavailable"
    runner.run(engine.dispose())


def test_a_title_with_a_control_character_is_422(api: Api, engine: Engine) -> None:
    """A NUL or another control character never reaches the database."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    response = api.call(
        "POST",
        "/api/v1/runs",
        actor=ACTOR,
        json={"device_id": "r1", "title": "a\u0000b"},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["errors"][0]["loc"] == ["body", "title"]
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM hco.change_set")).scalar_one()
            == 0
        )


def test_a_run_id_with_a_nul_is_404(api: Api) -> None:
    """No run id holds a NUL, so the API does not ask the database."""
    for path in ("/api/v1/runs/%00", "/api/v1/runs/%00/diff"):
        assert api.call("GET", path).status_code == HTTPStatus.NOT_FOUND


def test_a_run_with_no_checkpoint_has_no_unknowns(api: Api) -> None:
    """A run that no graph started shows `unknowns` as null, not an error."""
    run_id = draft_run(api)

    shown = api.call("GET", f"/api/v1/runs/{run_id}")

    assert shown.status_code == HTTPStatus.OK
    assert shown.json()["unknowns"] is None


def test_an_unreadable_checkpoint_gives_no_unknowns_and_a_log_line(api: Api) -> None:
    """The run still shows, and the log names the error class only."""
    run_id = create_run(api)
    api.app = create_app(
        CoreSettings(database_url=SecretStr(UNREACHABLE_URL)), api.deps
    )

    with capture_logs() as lines:
        shown = api.call("GET", f"/api/v1/runs/{run_id}")

    assert shown.json()["unknowns"] is None
    assert shown.json()["state"] == RunState.AWAITING_APPROVAL
    assert {
        "event": "unknowns_unavailable",
        "log_level": "error",
        "run_id": run_id,
        "error": "OperationalError",
    } in lines


def test_a_hostile_error_of_one_megabyte_is_scrubbed_in_linear_time() -> None:
    """Device text is untrusted, so the scrub must not stall the event loop."""
    hostile = "banner motd ^C " + "a://" * 250_000
    started = time.perf_counter()

    shown = run_errors({"errors": [hostile] * MAX_ERRORS})

    assert time.perf_counter() - started < HOSTILE_SECONDS
    assert all(len(item) <= MAX_ERROR_CHARACTERS for item in shown)


def test_a_url_cut_at_the_bound_shows_no_part_of_its_credentials() -> None:
    """A URL that the input bound cuts in its user information is not shown."""
    error = (
        "x " * (MAX_ERROR_CHARACTERS * 2 - 10)
        + "postgresql://hco:"
        + "s" * 5000
        + "@db"
    )

    shown = run_errors({"errors": [error]})

    assert "hco:" not in shown[0]
