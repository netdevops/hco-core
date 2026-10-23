"""The device routes: sync from the source of truth, list, and show."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, TextClause, text
from structlog.testing import capture_logs

from hco_core.api.app import create_app
from hco_core.api.errors import PROBLEM_JSON
from hco_core.api.routers import devices
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.settings import CoreSettings
from tests.api.conftest import ACTOR, Api, write_inventory
from tests.api.test_health import UNREACHABLE_URL

UNKNOWN_PLATFORM = """devices:
  - id: r9
    name: edge-9
    platform_name: NOT_A_PLATFORM
    mgmt_address: 10.0.0.9
"""
LARGE_INVENTORY = 6000
"""More devices than one statement takes, at 13 parameters for each."""


def test_sync_writes_each_device_of_the_source_of_truth(api: Api) -> None:
    """A sync stores the inventory and lists it in file order."""
    synced = api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    listed = api.call("GET", "/api/v1/devices")

    assert synced.status_code == HTTPStatus.OK
    assert synced.json() == {"synced": 2, "skipped": {}}
    devices = listed.json()["items"]
    assert [(item["sot_device_id"], item["name"]) for item in devices] == [
        ("r1", "edge-1"),
        ("r2", "edge-2"),
    ]
    assert devices[0]["platform_name"] == "CISCO_IOS"
    assert devices[0]["sot_name"] == "yaml"
    assert listed.json()["next_cursor"] is None


def test_sync_again_updates_the_rows_and_adds_none(api: Api, engine: Engine) -> None:
    """A second sync keeps one row for each source of truth id."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    write_inventory(
        api.root,
        (api.root / "inventory.yaml").read_text().replace("edge-1", "edge-one"),
    )

    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    with engine.connect() as connection:
        names = connection.execute(
            text("SELECT name FROM hco.device ORDER BY sot_device_id")
        ).scalars()
        assert list(names) == ["edge-one", "edge-2"]


def test_sync_writes_one_audit_event_with_the_actor(api: Api, engine: Engine) -> None:
    """The sync is audited with the actor of the header and a request id."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT actor, surface, action, after, request_id FROM hco.audit_event"
            )
        ).one()
    assert row[:4] == (ACTOR, "api", "device.sync", {"count": 2})
    assert row.request_id


def test_sync_without_the_actor_header_is_401_naming_the_header(api: Api) -> None:
    """A mutating route needs `X-HCO-Actor`."""
    response = api.call("POST", "/api/v1/devices/sync")

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["type"] == "urn:hco-core:problem:missing-actor"
    assert "X-HCO-Actor" in response.json()["detail"]


def test_an_actor_name_with_white_space_at_an_end_is_400(api: Api) -> None:
    """The header passes the name rule of the approval service."""
    response = api.call("POST", "/api/v1/devices/sync", actor="alice ")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["type"] == "urn:hco-core:problem:invalid-actor"


def test_an_unknown_platform_is_400_naming_the_device(api: Api) -> None:
    """A device with an unknown platform stops the sync, and nothing is written."""
    write_inventory(api.root, UNKNOWN_PLATFORM)

    response = api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["type"] == "urn:hco-core:problem:invalid-inventory"
    assert "'r9'" in response.json()["detail"]
    assert api.call("GET", "/api/v1/devices").json()["items"] == []


def test_an_empty_inventory_lists_zero_devices(api: Api) -> None:
    """An empty source of truth syncs zero devices and lists an empty page."""
    write_inventory(api.root, "devices: []\n")

    synced = api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    listed = api.call("GET", "/api/v1/devices")

    assert synced.json() == {"synced": 0, "skipped": {}}
    assert listed.json() == {"items": [], "next_cursor": None}


def test_the_list_pages_with_a_cursor(api: Api) -> None:
    """Each page holds at most `limit` devices and a cursor to the next page."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    first = api.call("GET", "/api/v1/devices?limit=1").json()
    second = api.call(
        "GET", f"/api/v1/devices?limit=1&cursor={first['next_cursor']}"
    ).json()

    assert [item["sot_device_id"] for item in first["items"]] == ["r1"]
    assert [item["sot_device_id"] for item in second["items"]] == ["r2"]
    assert second["next_cursor"] is None


def test_a_limit_out_of_range_is_a_422_problem(api: Api) -> None:
    """A query that fails the schema is a problem document with each place."""
    response = api.call("GET", "/api/v1/devices?limit=0")

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["errors"][0]["loc"] == ["query", "limit"]
    assert "input" not in response.json()["errors"][0]


def test_show_finds_a_device_by_its_id_or_its_source_of_truth_id(api: Api) -> None:
    """The id in the path is the row id or the id in the source of truth."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    row_id = api.call("GET", "/api/v1/devices").json()["items"][0]["id"]

    by_row = api.call("GET", f"/api/v1/devices/{row_id}")
    by_sot = api.call("GET", "/api/v1/devices/r1")

    assert by_row.status_code == HTTPStatus.OK
    assert by_row.json() == by_sot.json()
    assert by_row.json()["mgmt_address"] == "10.0.0.1"


def test_an_unknown_device_is_404_naming_the_id(api: Api) -> None:
    """The problem names the id that the request gave."""
    response = api.call("GET", "/api/v1/devices/missing")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.headers["content-type"] == PROBLEM_JSON
    assert "'missing'" in response.json()["detail"]


SOT_DETAIL = (
    "the source of truth is not readable; check HCO_SOT_PATH, or "
    "HCO_NAUTOBOT_URL and HCO_NAUTOBOT_TOKEN, and read the server log"
)


def remove_inventory(path: Path) -> None:
    """Remove the inventory file."""
    path.unlink()


def lock_inventory(path: Path) -> None:
    """Take every permission from the inventory file."""
    path.chmod(0)


@pytest.mark.parametrize(
    ("break_inventory", "cause", "reason"),
    [
        (remove_inventory, "FileNotFoundError", "ENOENT"),
        (lock_inventory, "PermissionError", "EACCES"),
    ],
    ids=["missing", "mode-000"],
)
def test_an_unreadable_inventory_is_503_naming_the_setting_and_the_cause(
    api: Api, break_inventory: Callable[[Path], None], cause: str, reason: str
) -> None:
    """The detail names the setting, and the log names the cause with no path."""
    inventory = api.root / "inventory.yaml"
    break_inventory(inventory)

    with capture_logs() as lines:
        response = api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    inventory.unlink(missing_ok=True)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["type"] == "urn:hco-core:problem:sot-unavailable"
    assert response.json()["detail"] == SOT_DETAIL
    assert {
        "event": "request_failed",
        "log_level": "error",
        "error": "SourceOfTruthUnavailableError",
        "cause": cause,
        "reason": reason,
    } in lines
    assert str(api.root) not in response.text
    assert str(api.root) not in repr(lines)


def test_a_sync_larger_than_one_statement_writes_every_device(
    api: Api, engine: Engine
) -> None:
    """6000 devices pass the parameter limit of one statement, with one audit row."""
    devices = "".join(
        f"  - {{id: d{index}, name: d{index}, platform_name: CISCO_IOS, mgmt_address: 10.0.0.1}}\n"
        for index in range(LARGE_INVENTORY)
    )
    write_inventory(api.root, f"devices:\n{devices}")

    response = api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    assert response.json() == {"synced": LARGE_INVENTORY, "skipped": {}}
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM hco.device")).scalar_one()
            == LARGE_INVENTORY
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM hco.audit_event")
            ).scalar_one()
            == 1
        )


def failing_statement(sot_name: str, records: object) -> TextClause:
    """Return a statement that the database refuses."""
    del sot_name, records
    return text("SELECT 1/0")


def test_a_failed_sync_logs_the_count_and_no_row(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database error in the sync is a 500 problem, and the log has no device."""
    monkeypatch.setattr(devices, "_upsert", failing_statement)

    with capture_logs() as lines:
        response = api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json()["type"] == "urn:hco-core:problem:device-sync-failed"
    assert {
        "event": "device_sync_failed",
        "log_level": "error",
        "count": 2,
        "error": "DataError",
    } in lines
    assert "edge-1" not in repr(lines)


@pytest.mark.parametrize("cursor", ["%00", "not-a-ulid", "01JDEVICE0000000000000000a"])
def test_a_cursor_that_is_not_a_ulid_is_a_422_problem(api: Api, cursor: str) -> None:
    """The cursor is the id of a device row."""
    response = api.call("GET", f"/api/v1/devices?cursor={cursor}")

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["errors"][0]["loc"] == ["query", "cursor"]


@pytest.mark.parametrize("device_id", ["%00", "r1%00"])
def test_an_id_with_a_nul_is_404(api: Api, device_id: str) -> None:
    """No device id holds a NUL, so the API does not ask the database."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    response = api.call("GET", f"/api/v1/devices/{device_id}")

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_the_row_id_wins_over_a_source_of_truth_id(api: Api) -> None:
    """A source of truth id equal to the row id of another device does not shadow it."""
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    row_id = api.call("GET", "/api/v1/devices/r1").json()["id"]
    shadow = f"  - {{id: {row_id}, name: shadow, platform_name: CISCO_IOS, mgmt_address: 10.0.0.9}}\n"
    write_inventory(api.root, (api.root / "inventory.yaml").read_text() + shadow)
    api.call("POST", "/api/v1/devices/sync", actor=ACTOR)

    response = api.call("GET", f"/api/v1/devices/{row_id}")

    assert response.json()["sot_device_id"] == "r1"


def test_the_list_and_the_sync_are_503_when_the_database_is_down(
    api: Api, runner: asyncio.Runner
) -> None:
    """A database that does not answer gives the same problem as `/health`."""
    engine = create_database_engine(
        CoreSettings(database_url=SecretStr(UNREACHABLE_URL))
    )
    deps = replace(api.deps, engine=engine, sessions=create_sessionmaker(engine))
    down = Api(runner, create_app(CoreSettings(), deps), deps, api.root)

    for method, path in (
        ("GET", "/api/v1/devices"),
        ("GET", "/api/v1/devices/r1"),
        ("POST", "/api/v1/devices/sync"),
    ):
        response = down.call(method, path, actor=ACTOR)

        assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.json()["type"] == "urn:hco-core:problem:database-unavailable"
    runner.run(engine.dispose())
