"""The repository carries the shared gate that every pull request must pass."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from hco_core.settings import CoreSettings

ROOT = Path(__file__).resolve().parent.parent
CHANGE_TYPES = {
    "added",
    "changed",
    "fixed",
    "removed",
    "security",
    "dependencies",
    "documentation",
}


def pyproject() -> dict[str, Any]:
    """Return the parsed `pyproject.toml`."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_lock_file_is_committed() -> None:
    """A committed lock makes `poetry install` reproducible."""
    assert (ROOT / "poetry.lock").is_file()


def test_pyright_runs_in_strict_mode() -> None:
    """Pyright strict finds `Protocol` errors that mypy misses."""
    assert pyproject()["tool"]["pyright"]["typeCheckingMode"] == "strict"


def test_coverage_floor_is_95_percent() -> None:
    """A run with `--cov` fails below the floor."""
    assert pyproject()["tool"]["coverage"]["report"]["fail_under"] == 95  # ruff: ignore[magic-value-comparison]


def test_towncrier_reads_fragments_of_the_seven_shared_types() -> None:
    """Fragments do not conflict on rebase the way one changelog file does."""
    towncrier = pyproject()["tool"]["towncrier"]
    assert towncrier["directory"] == "changes"
    assert {kind["directory"] for kind in towncrier["type"]} == CHANGE_TYPES
    assert (ROOT / towncrier["template"]).is_file()


def test_ci_holds_the_validation_gate_at_full_branch_coverage() -> None:
    """An untested branch in the gate is an untested path to a router."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf8")
    assert (
        "pytest -q tests/engine/test_validation.py --cov=hco_core.engine.validation"
        " --cov-branch --cov-report=term-missing --cov-fail-under=100"
    ) in workflow


def test_ci_gives_the_tests_a_postgres_service() -> None:
    """The database tests fail without Postgres, so CI must start one."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf8")
    assert "image: pgvector/pgvector:pg16" in workflow
    assert "HCO_DATABASE_URL: postgresql://hco:hco@localhost:5432/hco" in workflow


def test_env_example_names_every_core_setting() -> None:
    """A contributor finds each `HCO_` variable in `.env.example`."""
    example = (ROOT / ".env.example").read_text(encoding="utf8")
    for name in CoreSettings.model_fields:
        assert f"HCO_{name.upper()}=" in example


def test_ci_runs_the_interrupts_lint_rule_on_the_package() -> None:
    """A second or a conditional `interrupt()` fails the build (AGENTS.md rule 4)."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf8")
    assert "poetry run hco-core lint-interrupts hco_core" in workflow
