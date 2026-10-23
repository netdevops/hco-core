"""The Python examples of the README and of `docs/` must run.

Written after CI caught a README code block that neither compiled under the
formatter nor had ever been executed. Documentation that drifts from the code
is worse than none: a reader trusts it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")))


def python_blocks() -> list[tuple[str, Path]]:
    """Return every ```python fenced block, with the file that holds it."""
    return [
        (block, path)
        for path in DOCUMENTS
        for block in re.findall(
            r"```python\n(.*?)```", path.read_text(encoding="utf8"), re.DOTALL
        )
    ]


def example_id(value: object) -> str:
    """Name a test case by the file of its example."""
    return str(value.relative_to(ROOT)) if isinstance(value, Path) else "block"


def test_the_documents_have_examples() -> None:
    """Guard the guard: a regex that silently matches nothing proves nothing."""
    assert python_blocks(), "no python blocks found -- has the fence style changed?"


@pytest.mark.parametrize(("block", "path"), python_blocks(), ids=example_id)
def test_each_example_executes(
    block: str, path: Path, request: pytest.FixtureRequest
) -> None:
    """Run each block on its own, with no imports the reader is not shown.

    A block that uses the database runs on the test database, and the tables
    are emptied after it.
    """
    if "hco_core.db" in block:
        request.getfixturevalue("clean_tables")
    exec(compile(block, str(path), "exec"), {"__name__": "__docs__"})  # ruff: ignore[exec-builtin]
