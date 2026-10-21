"""The device routes: sync from the source of truth, list, and show."""

from http import HTTPStatus

from sqlalchemy import Engine, text

from hco_core.api.errors import PROBLEM_JSON
from tests.api.conftest import ACTOR, Api, write_inventory

UNKNOWN_PLATFORM = """devices:
  - id: r9
    name: edge-9
    platform_name: NOT_A_PLATFORM
    mgmt_address: 10.0.0.9
"""


def test_sync_writes_each_device_of_the_source_of_truth(api: Api) -> None:
    """A sync stores the inventory and lists it in file order."""
    synced = api.call("POST", "/api/v1/devices/sync", actor=ACTOR)
    listed = api.call("GET", "/api/v1/devices")

    assert synced.status_code == HTTPStatus.OK
    assert synced.json() == {"synced": 2}
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

    assert synced.json() == {"synced": 0}
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
