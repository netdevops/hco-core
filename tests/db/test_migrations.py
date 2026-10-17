"""The initial migration makes the schema of docs/spec/03 (AC-12)."""

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tests.db.conftest import TABLES

SECRET_NAMES = ("password", "secret", "token", "key")
ULID_LENGTH = 26

pytestmark = pytest.mark.usefixtures("clean_tables")


def test_the_eight_tables_are_in_schema_hco(engine: Engine) -> None:
    """Every Phase 1 core table exists in schema `hco`."""
    assert set(TABLES) <= set(inspect(engine).get_table_names(schema="hco"))


@pytest.mark.parametrize("table", TABLES)
def test_each_table_has_a_ulid_key_timestamps_and_a_nullable_tenant(
    engine: Engine, table: str
) -> None:
    """The conventions of docs/spec/03 hold for every table."""
    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns(table, "hco")}

    assert inspector.get_pk_constraint(table, "hco")["constrained_columns"] == ["id"]
    assert getattr(columns["id"]["type"], "length", None) == ULID_LENGTH
    assert columns["tenant_id"]["nullable"]
    assert getattr(columns["tenant_id"]["type"], "length", None) == ULID_LENGTH
    for name in ("created_at", "updated_at"):
        column_type = columns[name]["type"]
        assert isinstance(column_type, TIMESTAMP)
        assert column_type.timezone
        assert not columns[name]["nullable"]
        assert columns[name]["default"] == "now()"


def test_no_column_with_a_secret_name_has_a_plain_string_type(engine: Engine) -> None:
    """A credential never sits in a plain string column."""
    with engine.connect() as connection:
        offenders = connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'hco' AND column_name = ANY(:names) "
                "AND data_type IN ('character varying', 'character', 'text')"
            ),
            {"names": list(SECRET_NAMES)},
        ).all()

    assert offenders == []


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("approval_request", ["change_set_id"]),
        ("approval_decision", ["request_id", "actor"]),
        ("notification_delivery", ["run_id", "channel", "purpose"]),
        ("device", ["tenant_id", "sot_name", "sot_device_id"]),
    ],
)
def test_the_unique_constraints_exist(
    engine: Engine, table: str, columns: list[str]
) -> None:
    """One request per change set, one decision per person, one delivery per purpose."""
    constraints = inspect(engine).get_unique_constraints(table, "hco")

    assert columns in [constraint["column_names"] for constraint in constraints]
