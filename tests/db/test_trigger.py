"""The database refuses a decision by the requester (AC-7).

These tests insert directly, past any service, as docs/spec/03 requires.
"""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

REQUESTER = "alice"
APPROVER = "bob"
CHANGE_SET_ID = "01JCHANGESET0000000000000A"
REQUEST_ID = "01JREQUEST000000000000000A"

pytestmark = pytest.mark.usefixtures("clean_tables")


@pytest.fixture
def approval_request(engine: Engine) -> str:
    """Insert a pending approval request by the requester."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO hco.change_set "
                "(id, title, requested_by, transaction_class, state) "
                "VALUES (:id, 'change', :requester, 'strict', 'DRAFT')"
            ),
            {"id": CHANGE_SET_ID, "requester": REQUESTER},
        )
        connection.execute(
            text(
                "INSERT INTO hco.approval_request "
                "(id, change_set_id, state, required_count, requester, "
                "expires_at, interrupt_key) VALUES (:id, :change_set_id, "
                "'pending', 1, :requester, now() + interval '72 hours', 'k')"
            ),
            {
                "id": REQUEST_ID,
                "change_set_id": CHANGE_SET_ID,
                "requester": REQUESTER,
            },
        )
    return REQUEST_ID


def decide(engine: Engine, decision_id: str, request_id: str, actor: str) -> None:
    """Insert one approve decision directly."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO hco.approval_decision "
                "(id, request_id, actor, actor_kind, decision, surface) "
                "VALUES (:id, :request_id, :actor, 'user', 'approve', 'api')"
            ),
            {"id": decision_id, "request_id": request_id, "actor": actor},
        )


def test_the_requester_cannot_insert_a_decision(
    engine: Engine, approval_request: str
) -> None:
    """The trigger raises on a direct INSERT by the requester."""
    with pytest.raises(DBAPIError, match="may not approve their own change"):
        decide(engine, "01JDECISION00000000000000A", approval_request, REQUESTER)


def test_another_actor_can_insert_a_decision(
    engine: Engine, approval_request: str
) -> None:
    """The trigger lets a different person decide."""
    decide(engine, "01JDECISION00000000000000A", approval_request, APPROVER)

    with engine.connect() as connection:
        actors = connection.scalars(
            text("SELECT actor FROM hco.approval_decision")
        ).all()
    assert actors == [APPROVER]


def test_a_decision_cannot_change_its_actor_to_the_requester(
    engine: Engine, approval_request: str
) -> None:
    """An UPDATE cannot go around the INSERT check."""
    decide(engine, "01JDECISION00000000000000A", approval_request, APPROVER)

    with (
        pytest.raises(DBAPIError, match="may not approve their own change"),
        engine.begin() as connection,
    ):
        connection.execute(
            text("UPDATE hco.approval_decision SET actor = :actor"),
            {"actor": REQUESTER},
        )


def test_one_actor_cannot_decide_twice(engine: Engine, approval_request: str) -> None:
    """`UNIQUE (request_id, actor)` stops one person from filling a quorum."""
    decide(engine, "01JDECISION00000000000000A", approval_request, APPROVER)

    with pytest.raises(IntegrityError):
        decide(engine, "01JDECISION00000000000000B", approval_request, APPROVER)


def test_the_requester_of_a_request_cannot_change(
    engine: Engine, approval_request: str
) -> None:
    """An UPDATE cannot make an approver the requester after the fact."""
    decide(engine, "01JDECISION00000000000000A", approval_request, APPROVER)

    with (
        pytest.raises(DBAPIError, match="requester of approval request"),
        engine.begin() as connection,
    ):
        connection.execute(
            text("UPDATE hco.approval_request SET requester = :requester"),
            {"requester": APPROVER},
        )


def test_an_upsert_cannot_change_the_requester(
    engine: Engine, approval_request: str
) -> None:
    """`ON CONFLICT ... DO UPDATE` cannot go around the requester rule."""
    decide(engine, "01JDECISION00000000000000A", approval_request, APPROVER)

    with (
        pytest.raises(DBAPIError, match="requester of approval request"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO hco.approval_request "
                "(id, change_set_id, state, required_count, requester, "
                "expires_at, interrupt_key) VALUES ('01JREQUEST000000000000000B', "
                ":change_set_id, 'pending', 1, :requester, now(), 'k') "
                "ON CONFLICT (change_set_id) DO UPDATE "
                "SET requester = EXCLUDED.requester"
            ),
            {"change_set_id": CHANGE_SET_ID, "requester": APPROVER},
        )


@pytest.mark.parametrize(
    "actor", ["alice ", " alice", "", "alice\t", "\talice", "alice\n", "alice\u00a0"]
)
def test_an_actor_with_padding_or_no_text_is_refused(
    engine: Engine, approval_request: str, actor: str
) -> None:
    """White space cannot make the requester look like another person."""
    with pytest.raises(
        IntegrityError, match='constraint "ck_approval_decision_actor_trimmed"'
    ):
        decide(engine, "01JDECISION00000000000000A", approval_request, actor)


PADDED_CHANGE_SET = (
    "INSERT INTO hco.change_set (id, title, requested_by, transaction_class, state) "
    "VALUES ('01JCHANGESET0000000000000B', 'change', 'alice ', 'strict', 'DRAFT')"
)
PADDED_REQUEST = (
    "WITH change AS (INSERT INTO hco.change_set "
    "(id, title, requested_by, transaction_class, state) VALUES "
    "('01JCHANGESET0000000000000B', 'change', 'alice', 'strict', 'DRAFT') "
    "RETURNING id) INSERT INTO hco.approval_request (id, change_set_id, state, "
    "required_count, requester, expires_at, interrupt_key) SELECT "
    "'01JREQUEST000000000000000B', id, 'pending', 1, 'alice ', now(), 'k' "
    "FROM change"
)


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (PADDED_CHANGE_SET, "ck_change_set_requested_by_trimmed"),
        (
            PADDED_CHANGE_SET.replace("'alice '", "E'alice\\t'"),
            "ck_change_set_requested_by_trimmed",
        ),
        (PADDED_REQUEST, "ck_approval_request_requester_trimmed"),
    ],
)
def test_a_requester_with_padding_is_refused(
    engine: Engine, statement: str, constraint: str
) -> None:
    """The requester names have no padding, so the trigger compares like with like."""
    with (
        pytest.raises(IntegrityError, match=f'constraint "{constraint}"'),
        engine.begin() as connection,
    ):
        connection.execute(text(statement))
