"""The `hco-core` command: each command against a fake API (AC-10)."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest
from typer.testing import CliRunner, Result

from hco_core.cli import app as cli

RUN_ID = "01JRUN000000000000000000AA"
REQUEST_ID = "01JREQUEST0000000000000AAA"
CHANGE_SET_ID = "01JCHANGESET0000000000000A"
DEVICE_ROW_ID = "01JDEVICE00000000000000000"
CREATED = "2026-10-22T15:11:56Z"
API_URL = "http://api.test:8080"
USAGE_ERROR = 2

type Reply = httpx.Response | Callable[[httpx.Request], httpx.Response]


def run_detail(
    state: str = "AWAITING_APPROVAL", **changes: object
) -> dict[str, object]:
    """Return a `GET /runs/{id}` body."""
    return {
        "id": RUN_ID,
        "change_set_id": CHANGE_SET_ID,
        "device_id": DEVICE_ROW_ID,
        "sot_device_id": "r1",
        "device_name": "edge-1",
        "state": state,
        "requested_by": "alice",
        "risk_tier_floor": None,
        "risk_tier_final": "HIGH",
        "created_at": CREATED,
        "updated_at": CREATED,
        "plan_source": "deterministic",
        "change_fingerprint": "945957d1",
        "unknowns": ["no risk assessment"],
        "errors": [],
        **changes,
    }


def run_summary() -> dict[str, object]:
    """Return one item of a `GET /runs` page."""
    detail_only = {"plan_source", "change_fingerprint", "unknowns", "errors"}
    return {key: value for key, value in run_detail().items() if key not in detail_only}


def approval(
    state: str = "pending", run_state: str = "AWAITING_APPROVAL", approvers: int = 0
) -> dict[str, object]:
    """Return an approval detail with the approvals of bob, then carol."""
    return {
        "id": REQUEST_ID,
        "change_set_id": CHANGE_SET_ID,
        "run_id": RUN_ID,
        "state": state,
        "required_count": 2,
        "requester": "alice",
        "expires_at": CREATED,
        "run_state": run_state,
        "decisions": [
            {
                "id": f"01JDECISION00000000000000{index}",
                "request_id": REQUEST_ID,
                "actor": actor,
                "actor_kind": "user",
                "decision": "approve",
                "surface": "api",
                "comment": "",
            }
            for index, actor in enumerate(("bob", "carol")[:approvers])
        ],
    }


def problem(status: int, code: str, detail: str) -> httpx.Response:
    """Return an RFC 9457 problem document as the API sends it."""
    return httpx.Response(
        status,
        json={
            "type": f"urn:hco-core:problem:{code}",
            "title": code,
            "status": status,
            "detail": detail,
        },
        headers={"Content-Type": "application/problem+json"},
    )


@dataclass
class FakeApi:
    """Answer each request from a list of replies for its method and path."""

    replies: dict[tuple[str, str], list[Reply]] = field(
        default_factory=dict[tuple[str, str], list[Reply]]
    )
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def on(self, method: str, path: str, *replies: Reply) -> None:
        """Answer the method and path with the replies, in order. The last repeats."""
        self.replies[method, f"/api/v1{path}"] = list(replies)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Return the next reply, or a 599 for a request that the test did not expect."""
        self.requests.append(request)
        queue = self.replies.get((request.method, request.url.path), [])
        if not queue:
            return httpx.Response(599, text="unexpected request")
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return reply(request) if callable(reply) else reply

    def body(self, index: int = -1) -> object:
        """Return the JSON body of a request that the API got."""
        return json.loads(self.requests[index].content)


@pytest.fixture
def api() -> FakeApi:
    """Give an empty fake API."""
    return FakeApi()


@pytest.fixture
def invoke(api: FakeApi) -> Callable[..., Result]:
    """Run the command against the fake API, with no `HCO_ACTOR`."""
    runner = CliRunner()

    def call(*args: str, env: dict[str, str | None] | None = None) -> Result:
        return runner.invoke(
            cli.app,
            list(args),
            obj=httpx.MockTransport(api.handle),
            env={"HCO_API_URL": API_URL, "HCO_ACTOR": None, **(env or {})},
        )

    return call


def test_run_create_waits_until_the_run_parks(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """The run id and `AWAITING_APPROVAL` are shown, and the actor goes in the header."""
    api.on(
        "POST",
        "/runs",
        httpx.Response(
            202,
            json={"run_id": RUN_ID, "change_set_id": CHANGE_SET_ID, "state": "DRAFT"},
        ),
    )
    api.on(
        "GET",
        f"/runs/{RUN_ID}",
        httpx.Response(200, json=run_detail("COLLECTING")),
        httpx.Response(200, json=run_detail()),
    )

    result = invoke(
        "run", "create", "--device", "r1", "--as", "alice", "--title", "ntp"
    )

    assert result.exit_code == 0, result.output
    assert f"Run {RUN_ID} on device edge-1 is AWAITING_APPROVAL" in result.output
    assert "hco-core approval list" in result.output
    assert api.body(0) == {"device_id": "r1", "title": "ntp"}
    assert api.requests[0].headers["X-HCO-Actor"] == "alice"
    assert str(api.requests[0].url).startswith(API_URL)


def test_run_create_reads_the_actor_from_hco_actor(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """With no `--as`, the header holds `HCO_ACTOR`. `--no-wait` shows the first state."""
    api.on(
        "POST",
        "/runs",
        httpx.Response(
            202,
            json={"run_id": RUN_ID, "change_set_id": CHANGE_SET_ID, "state": "DRAFT"},
        ),
    )

    result = invoke(
        "run", "create", "--device", "r1", "--no-wait", env={"HCO_ACTOR": "bob"}
    )

    assert result.exit_code == 0, result.output
    assert f"Run {RUN_ID} is DRAFT" in result.output
    assert api.requests[0].headers["X-HCO-Actor"] == "bob"
    assert api.body(0) == {"device_id": "r1"}


def test_run_create_stops_waiting_at_the_timeout(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """A run that does not park in time is shown with its state and how to read it."""
    api.on(
        "POST",
        "/runs",
        httpx.Response(
            202,
            json={"run_id": RUN_ID, "change_set_id": CHANGE_SET_ID, "state": "DRAFT"},
        ),
    )
    api.on("GET", f"/runs/{RUN_ID}", httpx.Response(200, json=run_detail("COLLECTING")))

    result = invoke(
        "run", "create", "--device", "r1", "--as", "alice", "--timeout", "0"
    )

    assert result.exit_code == 0, result.output
    assert f"Run {RUN_ID} is still COLLECTING" in result.output
    assert f"hco-core run show {RUN_ID}" in result.output


def test_a_blocked_run_says_where_to_read_the_errors(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """A run that does not park points to `run show`."""
    api.on(
        "POST",
        "/runs",
        httpx.Response(
            202,
            json={"run_id": RUN_ID, "change_set_id": CHANGE_SET_ID, "state": "DRAFT"},
        ),
    )
    api.on("GET", f"/runs/{RUN_ID}", httpx.Response(200, json=run_detail("BLOCKED")))

    result = invoke("run", "create", "--device", "r1", "--as", "alice")

    assert result.output == (
        f"Run {RUN_ID} on device edge-1 is BLOCKED. "
        f"Show the errors with: hco-core run show {RUN_ID}\n"
    )


def test_run_create_with_json_prints_the_run(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """`--json` prints the run as the API returns it."""
    api.on(
        "POST",
        "/runs",
        httpx.Response(
            202,
            json={"run_id": RUN_ID, "change_set_id": CHANGE_SET_ID, "state": "DRAFT"},
        ),
    )
    api.on("GET", f"/runs/{RUN_ID}", httpx.Response(200, json=run_detail()))

    result = invoke("run", "create", "--device", "r1", "--as", "alice", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == run_detail()


def test_a_missing_actor_names_the_header_and_the_options(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """A 401 is one sentence that names `--as`, `HCO_ACTOR`, and the header."""
    api.on(
        "POST",
        "/runs",
        problem(
            401, "missing-actor", "send the X-HCO-Actor header with your user name"
        ),
    )

    result = invoke("run", "create", "--device", "r1")

    assert result.exit_code == 1
    assert "X-HCO-Actor" not in api.requests[0].headers
    assert result.output.count("\n") == 1
    assert "--as" in result.output
    assert "HCO_ACTOR" in result.output
    assert "X-HCO-Actor" in result.output


def test_an_unknown_device_names_the_device(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """A 404 shows the detail of the API, which names the device."""
    api.on(
        "POST",
        "/runs",
        problem(404, "device-not-found", "no device 'missing'; sync the devices"),
    )

    result = invoke("run", "create", "--device", "missing", "--as", "alice")

    assert result.exit_code == 1
    assert result.output == "Error: no device 'missing'; sync the devices.\n"


def test_a_problem_with_json_is_printed_as_json(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """With `--json`, stdout holds the problem document and stderr the sentence."""
    api.on(
        "GET", f"/runs/{RUN_ID}", problem(404, "run-not-found", "no remediation run")
    )

    result = invoke("run", "show", RUN_ID, "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["type"] == "urn:hco-core:problem:run-not-found"
    assert result.stderr == "Error: no remediation run.\n"


def test_an_invalid_request_names_each_field(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """A 422 names the field and the reason."""
    response = problem(422, "invalid-request", "the request does not match the schema")
    body = {
        **response.json(),
        "errors": [{"loc": ["body", "comment"], "msg": "String is too long"}],
    }
    api.on("POST", f"/approvals/{REQUEST_ID}/decide", httpx.Response(422, json=body))

    result = invoke("approve", REQUEST_ID, "--as", "bob", "--comment", "x")

    assert result.exit_code == 1
    assert result.output == (
        "Error: the request does not match the schema "
        "(body.comment: String is too long).\n"
    )


def test_control_characters_in_a_detail_are_not_printed(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """The terminal gets no escape sequence from the API."""
    api.on("GET", "/devices", problem(500, "internal-error", "bad\x1b[31m\x07 text"))

    result = invoke("device", "list")

    assert result.exit_code == 1
    assert result.output == "Error: bad[31m text.\n"


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(502, text="<html>bad gateway</html>"),
        httpx.Response(502, json=["not", "a", "problem"]),
        httpx.Response(502, json={"type": "urn:x", "detail": None}),
    ],
)
def test_an_answer_that_is_not_a_problem_shows_the_status(
    api: FakeApi, invoke: Callable[..., Result], reply: httpx.Response
) -> None:
    """A proxy page or a broken body gives the HTTP status, not the body."""
    api.on("GET", "/devices", reply)

    result = invoke("device", "list")

    assert result.exit_code == 1
    assert (
        result.output == "Error: the API at http://api.test:8080 answered HTTP 502.\n"
    )


def test_an_answer_that_the_cli_cannot_read_names_the_versions(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """A body of a different shape is one sentence, not a traceback."""
    api.on("GET", "/devices", httpx.Response(200, json={"devices": []}))

    result = invoke("device", "list")

    assert result.exit_code == 1
    assert result.output == (
        "Error: the API at http://api.test:8080 sent an answer that this CLI "
        "cannot read; use the same hco-core version for both.\n"
    )


def test_an_unreachable_api_names_hco_api_url_and_hides_the_password() -> None:
    """A connection error names the setting, and the URL loses its user information."""
    result = CliRunner().invoke(
        cli.app,
        ["device", "list"],
        env={"HCO_API_URL": "http://user:s3cret@127.0.0.1:1"},
    )

    assert result.exit_code == 1
    assert "HCO_API_URL" in result.output
    assert "http://127.0.0.1:1" in result.output
    assert "s3cret" not in result.output
    assert "user" not in result.output
    assert result.output.count("\n") == 1


def test_run_list_shows_a_table_and_the_next_page(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """The table has one row for each run, and `--state` is a query parameter."""
    api.on(
        "GET",
        "/runs",
        httpx.Response(200, json={"items": [run_summary()], "next_cursor": RUN_ID}),
    )

    result = invoke("run", "list", "--state", "AWAITING_APPROVAL", "--limit", "1")

    assert result.exit_code == 0, result.output
    header, row, more = result.output.splitlines()
    assert header.split() == [
        "RUN",
        "DEVICE",
        "STATE",
        "TIER",
        "REQUESTED",
        "BY",
        "CREATED",
    ]
    assert row.split() == [
        RUN_ID,
        "edge-1",
        "AWAITING_APPROVAL",
        "HIGH",
        "alice",
        CREATED,
    ]
    assert more == f"More runs: add --cursor {RUN_ID}"
    assert api.requests[0].url.params == httpx.QueryParams(
        {"state": "AWAITING_APPROVAL", "limit": "1"}
    )


def test_run_list_says_when_there_is_no_run(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """An empty page gives one sentence, and `--json` gives the page."""
    api.on("GET", "/runs", httpx.Response(200, json={"items": [], "next_cursor": None}))

    assert invoke("run", "list").output == "No runs.\n"
    assert json.loads(invoke("run", "list", "--json").stdout) == {
        "items": [],
        "next_cursor": None,
    }


def test_run_show_prints_the_state_the_tier_and_the_unknowns(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """Each field is on its own line, and each error has a line."""
    api.on(
        "GET",
        f"/runs/{RUN_ID}",
        httpx.Response(200, json=run_detail("BLOCKED", errors=["first", "second"])),
    )

    result = invoke("run", "show", RUN_ID)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "State:          BLOCKED" in lines
    assert "Risk tier:      HIGH (floor: none)" in lines
    assert "Unknowns:       no risk assessment" in lines
    assert lines[-2:] == ["  - first", "  - second"]


@pytest.mark.parametrize(
    ("command", "path", "body"),
    [
        ("show", f"/runs/{RUN_ID}", run_detail()),
        (
            "diff",
            f"/runs/{RUN_ID}/diff",
            {"remediation": [], "rollback": [], "future": ["a"]},
        ),
    ],
)
def test_run_show_and_diff_with_json_print_the_answer(
    api: FakeApi,
    invoke: Callable[..., Result],
    command: str,
    path: str,
    body: dict[str, object],
) -> None:
    """`--json` prints the whole answer, also the future config of a diff."""
    api.on("GET", path, httpx.Response(200, json=body))

    result = invoke("run", command, RUN_ID, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == body


def test_run_diff_prints_the_remediation_and_the_rollback(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """The future config is in `--json` only."""
    api.on(
        "GET",
        f"/runs/{RUN_ID}/diff",
        httpx.Response(
            200,
            json={
                "remediation": ["interface Gi1", "  no shutdown\t"],
                "rollback": ["no ntp server 192.0.2.10"],
                "future": ["hostname edge-1"],
            },
        ),
    )

    result = invoke("run", "diff", RUN_ID)

    assert result.exit_code == 0, result.output
    assert result.output == (
        "Remediation:\n  interface Gi1\n    no shutdown\nRollback:\n  no ntp server 192.0.2.10\n"
    )


def test_run_diff_of_a_run_with_no_remediation_names_the_state(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """A 409 shows the state from the detail."""
    api.on(
        "GET",
        f"/runs/{RUN_ID}/diff",
        problem(
            409,
            "run-not-ready",
            f"run '{RUN_ID}' is BLOCKED; it has no remediation yet",
        ),
    )

    result = invoke("run", "diff", RUN_ID)

    assert result.exit_code == 1
    assert "is BLOCKED" in result.output


def test_approve_posts_to_the_decide_route_and_counts_the_approvals(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """The first of two approvals leaves the run parked; the second approves it."""
    api.on(
        "POST",
        f"/approvals/{REQUEST_ID}/decide",
        httpx.Response(200, json=approval(approvers=1)),
        httpx.Response(200, json=approval("approved", "APPROVED", approvers=2)),
    )

    first = invoke("approve", REQUEST_ID, "--as", "bob", "--comment", "checked")
    second = invoke("approve", REQUEST_ID, "--as", "carol")

    assert first.output == (
        f"Request {REQUEST_ID} is pending: 1 of 2 approvals. "
        f"Run {RUN_ID} is AWAITING_APPROVAL.\n"
    )
    assert second.output == (
        f"Request {REQUEST_ID} is approved: 2 of 2 approvals. Run {RUN_ID} is APPROVED.\n"
    )
    assert api.body(0) == {"decision": "approve", "comment": "checked"}
    assert api.body(1) == {"decision": "approve", "comment": ""}
    assert [request.headers["X-HCO-Actor"] for request in api.requests] == [
        "bob",
        "carol",
    ]


def test_the_requester_gets_the_rule_from_the_api(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """A 403 shows the requester rule."""
    api.on(
        "POST",
        f"/approvals/{REQUEST_ID}/decide",
        problem(
            403,
            "requester-may-not-approve",
            "requester 'alice' may not decide on their own change",
        ),
    )

    result = invoke("approve", REQUEST_ID, "--as", "alice")

    assert result.exit_code == 1
    assert result.output == (
        "Error: requester 'alice' may not decide on their own change.\n"
    )


def test_reject_needs_a_comment(api: FakeApi, invoke: Callable[..., Result]) -> None:
    """Without `--comment` nothing is sent; with it, the decision is `reject`."""
    api.on(
        "POST",
        f"/approvals/{REQUEST_ID}/decide",
        httpx.Response(200, json=approval("rejected", "REJECTED")),
    )

    refused = invoke("reject", REQUEST_ID, "--as", "bob")
    result = invoke("reject", REQUEST_ID, "--as", "bob", "--comment", "wrong vlan")

    assert refused.exit_code == USAGE_ERROR
    assert result.exit_code == 0, result.output
    assert api.body() == {"decision": "reject", "comment": "wrong vlan"}
    assert "is rejected" in result.output
    assert "REJECTED" in result.output


def test_cancel_posts_the_comment(api: FakeApi, invoke: Callable[..., Result]) -> None:
    """A cancel closes the request, and the run state is shown."""
    api.on(
        "POST",
        f"/approvals/{REQUEST_ID}/cancel",
        httpx.Response(200, json=approval("cancelled", "BLOCKED")),
    )

    result = invoke("cancel", REQUEST_ID, "--as", "bob", "--comment", "wrong device")

    assert result.exit_code == 0, result.output
    assert api.body() == {"comment": "wrong device"}
    assert result.output == (
        f"Request {REQUEST_ID} is cancelled: 0 of 2 approvals. Run {RUN_ID} is BLOCKED.\n"
    )


def test_approval_list_shows_the_pending_requests(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """Each row has the request, the run, the requester, and the approval count."""
    summary = {
        key: value
        for key, value in approval().items()
        if key not in {"run_state", "decisions"}
    }
    api.on(
        "GET",
        "/approvals",
        httpx.Response(200, json={"items": [summary], "next_cursor": None}),
        httpx.Response(200, json={"items": [], "next_cursor": None}),
    )

    result = invoke("approval", "list")
    empty = invoke("approval", "list")

    header, row = result.output.splitlines()
    assert header.split() == ["REQUEST", "RUN", "REQUESTER", "NEEDED", "EXPIRES"]
    assert row.split() == [REQUEST_ID, RUN_ID, "alice", "2", CREATED]
    assert empty.output == "No pending approval requests.\n"


def test_device_list_shows_the_devices(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """The table shows the source of truth id, which `run create --device` takes."""
    device: dict[str, object] = {
        "id": DEVICE_ROW_ID,
        "sot_name": "yaml",
        "sot_device_id": "r1",
        "name": "edge-1",
        "platform_name": "CISCO_IOS",
        "mgmt_address": "10.0.0.1",
        "role": "edge",
        "site": None,
        "criticality": "high",
        "tags": ["edge"],
        "in_maintenance": False,
        "metadata": {},
        "last_synced_at": CREATED,
    }
    api.on(
        "GET",
        "/devices",
        httpx.Response(200, json={"items": [device], "next_cursor": None}),
    )

    result = invoke("device", "list")

    header, row = result.output.splitlines()
    assert header.split() == [
        "DEVICE",
        "NAME",
        "PLATFORM",
        "ADDRESS",
        "SITE",
        "CRITICALITY",
    ]
    assert row.split() == ["r1", "edge-1", "CISCO_IOS", "10.0.0.1", "-", "high"]


def test_device_list_with_no_device_says_how_to_sync(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """The empty state names the command that reads the source of truth."""
    api.on(
        "GET", "/devices", httpx.Response(200, json={"items": [], "next_cursor": None})
    )

    result = invoke("device", "list")

    assert result.exit_code == 0
    assert (
        result.output
        == "No devices. Read the source of truth with: hco-core device sync\n"
    )


def test_device_sync_sends_the_actor_and_shows_the_count(
    api: FakeApi, invoke: Callable[..., Result]
) -> None:
    """The sync is a write, so it carries the actor."""
    api.on("POST", "/devices/sync", httpx.Response(200, json={"synced": 3}))

    result = invoke("device", "sync", "--as", "alice")

    assert result.exit_code == 0, result.output
    assert result.output == "Synced 3 devices.\n"
    assert api.requests[0].headers["X-HCO-Actor"] == "alice"


def test_an_invalid_setting_is_one_sentence() -> None:
    """A settings error names the variable and never shows its value."""
    result = CliRunner().invoke(
        cli.app, ["device", "list"], env={"HCO_DATABASE_URL": "mysql://u:s3cret@h/db"}
    )

    assert result.exit_code == 1
    assert "HCO_DATABASE_URL" in result.output
    assert "s3cret" not in result.output
    assert result.output.count("\n") == 1
