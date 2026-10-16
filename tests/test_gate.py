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
    assert (ROOT / "poetry.lock").is_file()


def test_pyright_runs_in_strict_mode() -> None:
    assert pyproject()["tool"]["pyright"]["typeCheckingMode"] == "strict"
