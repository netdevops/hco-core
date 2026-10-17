"""The repository carries the shared gate that every pull request must pass."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

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
