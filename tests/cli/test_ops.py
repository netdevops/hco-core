"""The operations commands: `db upgrade`, `serve api`, and `lint-interrupts`."""

from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect
from typer.testing import CliRunner

from hco_core.cli import app as cli
from hco_core.db.models import CHECKPOINT_TABLES, SCHEMA
from tests.conftest import TABLES

ALL_INTERFACES = "0.0.0.0"  # ruff: ignore[hardcoded-bind-all-interfaces]


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API listens on the loopback address by default."""
    calls: list[tuple[str, dict[str, object]]] = []

    def run(target: str, **options: object) -> None:
        calls.append((target, options))

    monkeypatch.setattr(cli.uvicorn, "run", run)

    default = CliRunner().invoke(cli.app, ["serve", "api"])
    chosen = CliRunner().invoke(
        cli.app, ["serve", "api", "--host", ALL_INTERFACES, "--port", "9000"]
    )

    assert default.exit_code == chosen.exit_code == 0
    assert calls == [
        (cli.APP_FACTORY, {"factory": True, "host": "127.0.0.1", "port": 8080}),
        (cli.APP_FACTORY, {"factory": True, "host": ALL_INTERFACES, "port": 9000}),
    ]


def test_lint_interrupts_runs_the_rule(tmp_path: Path) -> None:
    """The exit code is the exit code of the rule."""
    bad = tmp_path / "node.py"
    bad.write_text("def node(state):\n    if state:\n        interrupt(1)\n")

    clean = CliRunner().invoke(cli.app, ["lint-interrupts", "hco_core"])
    failed = CliRunner().invoke(cli.app, ["lint-interrupts", str(bad)])

    assert clean.exit_code == 0, clean.output
    assert failed.exit_code == 1
    assert "node.py" in failed.output
