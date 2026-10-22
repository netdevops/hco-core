"""The approval routes: list, show, decide, and cancel (AC-7, AC-10)."""

from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, text

from hco_core.api.app import create_app
from hco_core.api.deps import build_deps
from hco_core.api.errors import PROBLEM_JSON, PROBLEM_TYPE
from hco_core.domain.approval import ApprovalDecision, ApprovalRequest, RunState
from hco_core.domain.risk import RiskTier
from hco_core.services.audit import Actor
from hco_core.services.resume import RunResumer
from hco_core.services.runs import RunUpdate
from hco_core.settings import CoreSettings
from tests.api.conftest import ACTOR, Api
from tests.api.test_runs import create_run, draft_run
from tests.graphs.test_review import ENVIRON

APPROVALS = "/api/v1/approvals"
UNKNOWN = "01JNOSUCHREQUEST0000000000"
MAX_DETAIL = 80
SYSTEM = Actor("test", "system", "api")


def parked_request(api: Api) -> tuple[str, str]:
    """Create a run as alice that parks, and return the run id and its request id."""
    run_id = create_run(api)
    items = api.call("GET", APPROVALS).json()["items"]
    (request_id,) = [item["id"] for item in items if item["run_id"] == run_id]
    return run_id, request_id


def decide(
    api: Api, request_id: str, actor: str, decision: str = "approve"
) -> dict[str, object]:
    """Decide on the request as the actor, and return the response as JSON."""
    response = api.call(
        "POST",
        f"{APPROVALS}/{request_id}/decide",
        actor=actor,
        json={"decision": decision, "comment": f"{decision} by {actor}"},
    )
    return {"status": response.status_code, **response.json()}


@pytest.fixture
def resumed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count each call of the resume callback, by request id."""
    calls: list[str] = []
    original = RunResumer.resume_closed

    async def counted(
        self: RunResumer, request: ApprovalRequest, decision: ApprovalDecision | None
    ) -> None:
        calls.append(request.id)
        await original(self, request, decision)

    monkeypatch.setattr(RunResumer, "resume_closed", counted)
    return calls


def test_alice_requests_bob_and_carol_approve_and_the_run_is_approved(
    api: Api, resumed: list[str]
) -> None:
    """The whole loop through the API: create, park, decide, resume (AC-10)."""
    run_id, request_id = parked_request(api)

    shown = api.call("GET", f"{APPROVALS}/{request_id}").json()
    first = decide(api, request_id, "bob")
    second = decide(api, request_id, "carol")
    run = api.call("GET", f"/api/v1/runs/{run_id}").json()

    assert shown["requester"] == ACTOR
    assert shown["required_count"] == 2  # ruff: ignore[magic-value-comparison]
    assert shown["run_state"] == RunState.AWAITING_APPROVAL
    assert (first["status"], first["state"]) == (HTTPStatus.OK, "pending")
    assert first["run_state"] == RunState.AWAITING_APPROVAL
    assert (second["status"], second["state"]) == (HTTPStatus.OK, "approved")
    assert second["run_state"] == RunState.APPROVED
    decisions = api.call("GET", f"{APPROVALS}/{request_id}").json()["decisions"]
    assert [item["actor"] for item in decisions] == ["bob", "carol"]
    assert (run["state"], run["risk_tier_final"]) == (RunState.APPROVED, "HIGH")
    assert resumed == [request_id]


def test_the_requester_may_not_decide_on_her_own_run(api: Api, engine: Engine) -> None:
    """Alice gets 403, and no decision row is written (AC-7)."""
    _, request_id = parked_request(api)

    refused = decide(api, request_id, ACTOR)

    assert refused["status"] == HTTPStatus.FORBIDDEN
    assert refused["type"] == f"{PROBLEM_TYPE}requester-may-not-approve"
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM hco.approval_decision"))
        assert count.scalar_one() == 0


def test_a_second_decision_by_bob_is_409(api: Api) -> None:
    """One person counts once (AC-7)."""
    _, request_id = parked_request(api)
    decide(api, request_id, "bob")

    again = decide(api, request_id, "BOB")

    assert again["status"] == HTTPStatus.CONFLICT
    assert again["type"] == f"{PROBLEM_TYPE}duplicate-decision"


def test_one_rejection_rejects_the_run(api: Api, resumed: list[str]) -> None:
    """A reject closes the request at any tier, and the run resumes once."""
    run_id, request_id = parked_request(api)

    rejected = decide(api, request_id, "bob", "reject")

    assert (rejected["state"], rejected["run_state"]) == ("rejected", "REJECTED")
    assert api.call("GET", f"/api/v1/runs/{run_id}").json()["state"] == "REJECTED"
    assert resumed == [request_id]


def test_cancel_ends_the_run_blocked_and_refuses_a_later_decision(
    api: Api, resumed: list[str]
) -> None:
    """A cancel closes the request with no decision; a later decide is 409."""
    run_id, request_id = parked_request(api)

    cancelled = api.call(
        "POST", f"{APPROVALS}/{request_id}/cancel", actor="bob", json={}
    )
    late = decide(api, request_id, "carol")
    again = api.call("POST", f"{APPROVALS}/{request_id}/cancel", actor="carol")
    run = api.call("GET", f"/api/v1/runs/{run_id}").json()

    assert cancelled.status_code == HTTPStatus.OK
    assert (cancelled.json()["state"], cancelled.json()["run_state"]) == (
        "cancelled",
        RunState.BLOCKED,
    )
    assert (late["status"], late["type"]) == (
        HTTPStatus.CONFLICT,
        f"{PROBLEM_TYPE}approval-closed",
    )
    assert again.status_code == HTTPStatus.CONFLICT
    assert run["errors"] == ["record_decision: the approval request is cancelled"]
    assert resumed == [request_id]


def test_a_decision_on_a_closed_request_is_409(api: Api) -> None:
    """An approved request takes no more decisions."""
    _, request_id = parked_request(api)
    decide(api, request_id, "bob")
    decide(api, request_id, "carol")

    late = decide(api, request_id, "dave", "reject")

    assert (late["status"], late["type"]) == (
        HTTPStatus.CONFLICT,
        f"{PROBLEM_TYPE}approval-closed",
    )


def test_concurrent_decisions_resume_the_run_once(api: Api, resumed: list[str]) -> None:
    """Three approvers at once: two count, one is too late, and one resume runs."""
    run_id, request_id = parked_request(api)
    body = {"decision": "approve"}

    responses = api.call_together(
        "POST",
        [
            (f"{APPROVALS}/{request_id}/decide", actor, body)
            for actor in ("bob", "carol", "dave")
        ],
    )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [HTTPStatus.OK, HTTPStatus.OK, HTTPStatus.CONFLICT]
    assert resumed == [request_id]
    assert api.call("GET", f"/api/v1/runs/{run_id}").json()["state"] == "APPROVED"


def request_before_park(api: Api, state: RunState) -> tuple[str, str]:
    """Open a pending request on a run in the state, and return the run and request ids."""
    run_id = draft_run(api)
    api.runner.run(
        api.deps.review.runs.update_run(run_id, RunUpdate(state=state), SYSTEM)
    )
    change_set_id = api.call("GET", f"/api/v1/runs/{run_id}").json()["change_set_id"]
    request = api.runner.run(
        api.deps.review.approvals.upsert_request(
            change_set_id,
            RiskTier.LOW,
            1,
            SYSTEM.name,
            datetime.now(UTC) + timedelta(hours=1),
        )
    )
    return run_id, request.id


@pytest.mark.parametrize("state", [RunState.DRAFT, RunState.EVALUATING_POLICY])
def test_a_request_before_its_run_parks_is_hidden_and_refused_with_no_write(
    api: Api, engine: Engine, state: RunState
) -> None:
    """The graph opens the request before it parks; no route acts in that gap."""
    run_id, request_id = request_before_park(api, state)

    listed = [item["id"] for item in api.call("GET", APPROVALS).json()["items"]]
    decided = decide(api, request_id, "bob")
    cancelled = api.call("POST", f"{APPROVALS}/{request_id}/cancel", actor="bob")

    assert request_id not in listed
    assert (decided["status"], decided["type"]) == (
        HTTPStatus.CONFLICT,
        f"{PROBLEM_TYPE}run-not-awaiting-approval",
    )
    assert run_id in str(decided["detail"])
    assert cancelled.status_code == HTTPStatus.CONFLICT
    assert cancelled.json()["type"] == f"{PROBLEM_TYPE}run-not-awaiting-approval"
    shown = api.call("GET", f"{APPROVALS}/{request_id}").json()
    assert (shown["state"], shown["run_state"], shown["decisions"]) == (
        "pending",
        state,
        [],
    )
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM hco.approval_decision"))
        assert count.scalar_one() == 0


def test_a_pending_request_of_a_run_that_left_the_review_can_only_be_cancelled(
    api: Api,
) -> None:
    """A run that ended `BLOCKED` takes no decision, but its request can close."""
    _, request_id = request_before_park(api, RunState.BLOCKED)

    decided = decide(api, request_id, "bob")
    cancelled = api.call("POST", f"{APPROVALS}/{request_id}/cancel", actor="bob")

    assert decided["type"] == f"{PROBLEM_TYPE}run-not-awaiting-approval"
    assert cancelled.status_code == HTTPStatus.OK
    assert (cancelled.json()["state"], cancelled.json()["run_state"]) == (
        "cancelled",
        RunState.BLOCKED,
    )


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("GET", ""), ("POST", "/decide"), ("POST", "/cancel")],
)
@pytest.mark.parametrize(
    "request_id",
    [UNKNOWN, "not-an-id", "nul%00", pytest.param("x" * 5000, id="long")],
)
def test_an_unknown_approval_is_404(
    api: Api, method: str, suffix: str, request_id: str
) -> None:
    """An id that no request has, or that no request can have, is 404."""
    response = api.call(
        method,
        f"{APPROVALS}/{request_id}{suffix}",
        actor="bob",
        json={"decision": "approve"} if suffix == "/decide" else {},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.headers["content-type"] == PROBLEM_JSON
    assert response.json()["type"] == f"{PROBLEM_TYPE}approval-not-found"
    assert len(response.json()["detail"]) <= MAX_DETAIL


def test_decide_needs_the_actor_header_and_a_valid_decision(api: Api) -> None:
    """No actor is 401, an invisible character is 400, a bad decision is 422."""
    _, request_id = parked_request(api)
    path = f"{APPROVALS}/{request_id}/decide"

    anonymous = api.call("POST", path, json={"decision": "approve"})
    invisible = api.call("POST", path, actor="bob\u200b", json={"decision": "approve"})
    maybe = api.call("POST", path, actor="bob", json={"decision": "maybe"})
    anonymous_cancel = api.call("POST", f"{APPROVALS}/{request_id}/cancel")

    assert anonymous.status_code == HTTPStatus.UNAUTHORIZED
    assert invisible.status_code == HTTPStatus.BAD_REQUEST
    assert maybe.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert anonymous_cancel.status_code == HTTPStatus.UNAUTHORIZED
    assert api.call("GET", f"{APPROVALS}/{request_id}").json()["state"] == "pending"


def test_the_list_shows_pending_requests_newest_first_in_pages(api: Api) -> None:
    """A closed request leaves the list, and `next_cursor` gives the next page."""
    _, first = parked_request(api)
    _, second = parked_request(api)
    _, third = parked_request(api)
    api.call("POST", f"{APPROVALS}/{third}/cancel", actor="bob")

    page = api.call("GET", f"{APPROVALS}?limit=1").json()
    rest = api.call("GET", f"{APPROVALS}?limit=1&cursor={page['next_cursor']}").json()

    assert [item["id"] for item in page["items"]] == [second]
    assert [item["id"] for item in rest["items"]] == [first]
    assert rest["next_cursor"] is None


def test_build_deps_wires_the_resume_of_the_run(
    api: Api, settings: CoreSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app of `uvicorn` resumes a run when its request closes."""
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)
    deps = build_deps(
        settings.model_copy(
            update={
                "validation_key": SecretStr("k" * 32),
                "sot_path": api.root / "inventory.yaml",
                "transcripts_dir": api.root / "transcripts",
            }
        )
    )
    served = Api(api.runner, create_app(settings, deps), deps, api.root)
    run_id, request_id = parked_request(served)

    decide(served, request_id, "bob")
    decide(served, request_id, "carol")

    assert served.call("GET", f"/api/v1/runs/{run_id}").json()["state"] == "APPROVED"
    api.runner.run(deps.engine.dispose())
