"""The README's examples must run.

Written after CI caught a README code block that neither compiled under the
formatter nor had ever been executed. Documentation that drifts from the code
is worse than none: a reader trusts it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parent.parent / "README.md"


def python_blocks() -> list[str]:
    """Return every ```python fenced block in the README."""
    return re.findall(
        r"```python\n(.*?)```", README.read_text(encoding="utf8"), re.DOTALL
    )


def test_readme_has_examples() -> None:
    """Guard the guard: a regex that silently matches nothing proves nothing."""
    assert python_blocks(), "no python blocks found -- has the fence style changed?"


@pytest.mark.parametrize("block", python_blocks())
def test_readme_block_executes(block: str, request: pytest.FixtureRequest) -> None:
    """Each block runs on its own, with no imports the reader is not shown.

    A block that uses the database runs on the test database, and the tables
    are emptied after it.
    """
    if "hco_core.db" in block:
        request.getfixturevalue("clean_tables")
    exec(compile(block, "README.md", "exec"), {"__name__": "__readme__"})  # ruff: ignore[exec-builtin]
