"""The operations commands: `db upgrade`, `serve api`, and `lint-interrupts`."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, inspect
from typer.testing import CliRunner

from hco_core.cli import app as cli
from hco_core.db.models import CHECKPOINT_TABLES, SCHEMA
from hco_core.settings import DEVELOPMENT_VALIDATION_KEY
from tests.conftest import TABLES

ALL_INTERFACES = "0.0.0.0"  # ruff: ignore[hardcoded-bind-all-interfaces]
KEY = "k" * 32
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def served(
    monkeypatch: pytest.MonkeyPatch, restore_logging: None
) -> list[tuple[object, dict[str, object]]]:
    """Record each call of `uvicorn.run` in place of a server."""
    del restore_logging
    calls: list[tuple[object, dict[str, object]]] = []

    def run(target: object, **options: object) -> None:
        calls.append((target, options))

    monkeypatch.setattr(cli.uvicorn, "run", run)
    return calls


def test_db_upgrade_makes_the_tables_and_the_checkpointer_tables(
    database_url: str, engine: Engine
) -> None:
    """`db upgrade` runs the Alembic chain, then `setup_checkpointer`."""
    result = CliRunner().invoke(
        cli.app, ["db", "upgrade"], env={"HCO_DATABASE_URL": database_url}
    )

    assert result.exit_code == 0, result.output
    assert (
        result.output == "The database schema and the checkpointer tables are ready.\n"
    )
    tables = set(inspect(engine).get_table_names(schema=SCHEMA))
    assert tables >= {*TABLES, *CHECKPOINT_TABLES}


def test_db_upgrade_failure_names_the_setting_and_hides_the_password() -> None:
    """An unreachable database gives one sentence with no URL."""
    result = CliRunner().invoke(
        cli.app,
        ["db", "upgrade"],
        env={"HCO_DATABASE_URL": "postgresql://hco:s3cret@127.0.0.1:1/hco"},
    )

    assert result.exit_code == 1
    assert "HCO_DATABASE_URL" in result.output
    assert "s3cret" not in result.output
    assert "127.0.0.1" not in result.output
    assert result.output.count("\n") == 1


def test_serve_api_starts_uvicorn_with_the_app_factory(
    served: list[tuple[object, dict[str, object]]],
) -> None:
    """The API is built before uvicorn starts, on the loopback address by default."""
    env = {"HCO_VALIDATION_KEY": KEY}
    default = CliRunner().invoke(cli.app, ["serve", "api"], env=env)
    chosen = CliRunner().invoke(
        cli.app, ["serve", "api", "--host", ALL_INTERFACES, "--port", "9000"], env=env
    )

    assert default.exit_code == chosen.exit_code == 0
    assert [options for _, options in served] == [
        {"host": "127.0.0.1", "port": 8080, "log_config": None},
        {"host": ALL_INTERFACES, "port": 9000, "log_config": None},
    ]
    assert all(isinstance(target, FastAPI) for target, _ in served)


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"HCO_VALIDATION_KEY": None}, "HCO_VALIDATION_KEY"),
        (
            {"HCO_VALIDATION_KEY": KEY, "HCO_DATABASE_URL": "mysql://u:s3cret@h/db"},
            "HCO_DATABASE_URL",
        ),
        (
            {
                "HCO_VALIDATION_KEY": DEVELOPMENT_VALIDATION_KEY,
                "HCO_CONNECTION": "scrapli",
            },
            "HCO_CONNECTION",
        ),
        ({"HCO_VALIDATION_KEY": KEY, "HCO_CONNECTION": "netmiko"}, "HCO_CONNECTION"),
        ({"HCO_VALIDATION_KEY": KEY, "HCO_SOT": "nautobotx"}, "HCO_SOT"),
        ({"HCO_VALIDATION_KEY": "abc"}, "HCO_VALIDATION_KEY"),
    ],
)
def test_serve_api_stops_with_one_sentence_on_a_bad_setting(
    served: list[tuple[object, dict[str, object]]],
    env: dict[str, str | None],
    named: str,
) -> None:
    """No server starts, and no traceback or secret is printed."""
    result = CliRunner().invoke(cli.app, ["serve", "api"], env=env)

    assert result.exit_code == 1
    assert served == []
    assert named in result.output
    assert "s3cret" not in result.output
    assert result.output.count("\n") == 1


def test_db_upgrade_refuses_the_development_key_with_a_real_connection() -> None:
    """The refusal comes before the database is opened."""
    result = CliRunner().invoke(
        cli.app,
        ["db", "upgrade"],
        env={
            "HCO_VALIDATION_KEY": DEVELOPMENT_VALIDATION_KEY,
            "HCO_CONNECTION": "scrapli",
            "HCO_DATABASE_URL": "postgresql://hco:hco@127.0.0.1:1/hco",
        },
    )

    assert result.exit_code == 1
    assert "HCO_CONNECTION" in result.output
    assert "database upgrade failed" not in result.output


def test_the_development_env_file_holds_the_refused_key() -> None:
    """The refusal applies to the key that the repository publishes."""
    lines = (ROOT / "development" / ".env").read_text(encoding="utf8").splitlines()

    assert f"HCO_VALIDATION_KEY={DEVELOPMENT_VALIDATION_KEY}" in lines
    assert lines[0].startswith("# WARNING:")


def test_lint_interrupts_usage_names_the_command(tmp_path: Path) -> None:
    """A missing path gives the usage of `hco-core lint-interrupts`."""
    result = CliRunner().invoke(cli.app, ["lint-interrupts", str(tmp_path / "no.py")])

    assert result.exit_code == 2  # ruff: ignore[magic-value-comparison]
    assert "usage: hco-core lint-interrupts <path>" in result.output


def test_lint_interrupts_runs_the_rule(tmp_path: Path) -> None:
    """The exit code is the exit code of the rule."""
    bad = tmp_path / "node.py"
    bad.write_text("def node(state):\n    if state:\n        interrupt(1)\n")

    clean = CliRunner().invoke(cli.app, ["lint-interrupts", "hco_core"])
    failed = CliRunner().invoke(cli.app, ["lint-interrupts", str(bad)])

    assert clean.exit_code == 0, clean.output
    assert failed.exit_code == 1
    assert "node.py" in failed.output
