"""Run each Python example of the README and of `docs/`.

A reader copies these examples, so each one must run against the current code.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")))
PYTHON_FENCE = re.compile(r"^([ \t]*)```python\n(.*?)^\1```", re.DOTALL | re.MULTILINE)
OTHER_PYTHON_FENCE = re.compile(r"^[ \t]*```(py|pycon|python3)\s*$", re.MULTILINE)


def python_examples() -> list[tuple[str, Path, str]]:
    """Return each ```python block without its indent, its file, and a test id.

    The test id is the file and the position of the block in that file.
    """
    examples: list[tuple[str, Path, str]] = []
    for path in DOCUMENTS:
        text = path.read_text(encoding="utf8")
        for position, match in enumerate(PYTHON_FENCE.finditer(text)):
            block = textwrap.dedent(match.group(2))
            examples.append((block, path, f"{path.relative_to(ROOT)}:{position}"))
    return examples


EXAMPLES = python_examples()


def test_the_documents_have_examples() -> None:
    """Fail when the fence pattern finds no example."""
    assert EXAMPLES, "no python examples found; check the fence pattern"


@pytest.mark.parametrize(
    "path", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT))
)
def test_each_python_example_uses_the_python_fence(path: Path) -> None:
    """Refuse a Python example that this test does not run."""
    names = OTHER_PYTHON_FENCE.findall(path.read_text(encoding="utf8"))
    assert not names, f"use ```python, not ```{names[0]}"


@pytest.mark.parametrize(
    ("block", "path"),
    [(block, path) for block, path, _ in EXAMPLES],
    ids=[example_id for _, _, example_id in EXAMPLES],
)
def test_each_example_executes(
    block: str, path: Path, request: pytest.FixtureRequest
) -> None:
    """Run each block alone, with only the imports that the reader sees.

    For a block that uses the database, the `clean_tables` fixture empties the
    tables after the block.
    """
    if "hco_core.db" in block:
        request.getfixturevalue("clean_tables")
    exec(compile(block, str(path), "exec"), {"__name__": "__docs__"})  # ruff: ignore[exec-builtin]
