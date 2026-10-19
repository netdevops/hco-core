"""The initial migration makes the schema of docs/spec/03 (AC-12)."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pydantic import SecretStr
from sqlalchemy import CheckConstraint, Engine, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from hco_core.db.migrate import ALEMBIC_INI, downgrade, upgrade
from hco_core.db.models import Base
from hco_core.settings import CoreSettings
from tests.conftest import TABLES

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


def test_upgrade_downgrade_upgrade(engine: Engine, database_url: str) -> None:
    """The chain goes down to base and up to head again (AC-12)."""
    settings = CoreSettings(database_url=SecretStr(database_url))

    downgrade(settings, "base")
    assert set(TABLES).isdisjoint(inspect(engine).get_table_names(schema="hco"))
    with engine.connect() as connection:
        functions = connection.scalar(
            text("SELECT count(*) FROM pg_proc WHERE proname = 'reject_self_approval'")
        )
    assert functions == 0

    upgrade(settings)
    assert set(TABLES) <= set(inspect(engine).get_table_names(schema="hco"))


def test_the_models_match_the_migration(database_url: str) -> None:
    """The ORM models and the migrated schema have no difference.

    The user `hco` puts schema `hco` first in the search path, and reflection
    then names it the default schema. This engine starts in schema `public`.
    """
    public_engine = create_engine(
        database_url, connect_args={"options": "-c search_path=public"}
    )
    try:
        with public_engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"include_schemas": True, "version_table_schema": "hco"},
            )
            differences = compare_metadata(context, Base.metadata)
    finally:
        public_engine.dispose()

    assert differences == []


def test_alembic_without_a_connection_names_the_migrate_functions() -> None:
    """The `alembic` command alone gets a clear error, not a `KeyError`."""
    with pytest.raises(RuntimeError, match=r"hco_core\.db\.migrate\.upgrade\(\)"):
        command.upgrade(Config(ALEMBIC_INI), "head")


def test_the_check_names_match_the_models(engine: Engine) -> None:
    """Each `CHECK` in the database has the name that the models give it."""
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        expected = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        stored = {
            check["name"]
            for check in inspector.get_check_constraints(table.name, "hco")
        }

        assert stored == expected, table.name
