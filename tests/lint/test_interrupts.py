"""The interrupts lint rule keeps each node to one unconditional `interrupt()`."""

import inspect
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import textwrap
from pathlib import Path

import pytest

from hco_core.graphs import review
from hco_core.lint.interrupts import Violation, check, main

ONE_CALL = """
from langgraph.types import interrupt

async def request_approval(state, deps):
    request = await deps.approvals.upsert_request(state["change_set_id"])
    await deps.runs.update_run(state["run_id"], only_from={"EVALUATING_POLICY"})
    decision = interrupt({"request_id": request.id})
    return {"approval": decision}
"""

FAILING = {
    "two-calls": """
        def node(state):
            first = interrupt("a")
            second = interrupt("b")
            return {"a": first, "b": second}
    """,
    "if": """
        def node(state):
            if state["ask"]:
                return interrupt("a")
            return {}
    """,
    "else": """
        def node(state):
            if state["skip"]:
                return {}
            else:
                return interrupt("a")
    """,
    "for": """
        def node(state):
            for item in state["items"]:
                interrupt(item)
    """,
    "async-for": """
        async def node(state):
            async for item in state["items"]:
                interrupt(item)
    """,
    "while": """
        def node(state):
            while True:
                return interrupt("a")
    """,
    "try": """
        def node(state):
            try:
                return interrupt("a")
            except ValueError:
                return {}
    """,
    "with": """
        def node(state):
            with state["lock"]:
                return interrupt("a")
    """,
    "async-with": """
        async def node(state):
            async with state["lock"]:
                return interrupt("a")
    """,
    "match": """
        def node(state):
            match state["kind"]:
                case "ask":
                    return interrupt("a")
    """,
    "conditional-expression": """
        def node(state):
            return interrupt("a") if state["ask"] else {}
    """,
    "boolean-operator": """
        def node(state):
            return state.get("answer") or interrupt("a")
    """,
    "comprehension": """
        def node(state):
            return [interrupt(item) for item in state["items"]]
    """,
    "lambda": """
        def node(state):
            ask = lambda: interrupt("a")
            return ask()
    """,
    "module-level": """
        answer = interrupt("a")
    """,
    "class-body": """
        class Node:
            answer = interrupt("a")
    """,
    "alias": """
        from langgraph.types import interrupt as pause

        def node(state):
            if state["ask"]:
                return pause("a")
            return {}
    """,
    "attribute": """
        from langgraph import types

        def node(state):
            if state["ask"]:
                return types.interrupt("a")
            return {}
    """,
}


def write(tmp_path: Path, source: str, name: str = "nodes.py") -> Path:
    """Write the source to a file and return its path."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(source), encoding="utf8")
    return path


def test_one_unconditional_call_after_idempotent_calls_passes(tmp_path: Path) -> None:
    """Idempotent calls above the one `interrupt()` are allowed."""
    assert check([write(tmp_path, ONE_CALL)]) == []


def test_the_real_request_approval_passes(tmp_path: Path) -> None:
    """The review graph module, as it is, has no violation."""
    assert check([write(tmp_path, inspect.getsource(review))]) == []


@pytest.mark.parametrize("source", FAILING.values(), ids=FAILING.keys())
def test_a_second_or_a_conditional_call_fails(tmp_path: Path, source: str) -> None:
    """Each sample gives exactly one violation, on the offending call."""
    path = write(tmp_path, source)

    (violation,) = check([path])

    line = path.read_text(encoding="utf8").splitlines()[violation.line - 1]
    assert violation.path == path
    assert "interrupt(" in line or "pause(" in line


def test_a_violation_names_the_place_and_the_reason(tmp_path: Path) -> None:
    """The text shows the file, the line, the function, and the reason."""
    path = write(tmp_path, FAILING["if"])

    (violation,) = check([path])

    assert violation == Violation(path, 4, "node", "interrupt() inside if")
    assert str(violation) == f"{path}:4: node: interrupt() inside if"


def test_a_nested_function_is_checked_on_its_own(tmp_path: Path) -> None:
    """A call in an inner function does not count for the outer one."""
    source = """
        def build(graph):
            def node(state):
                return interrupt("a")
            graph.add_node("node", node)
            return interrupt("b")
    """
    assert check([write(tmp_path, source)]) == []


def test_a_directory_is_checked_recursively(tmp_path: Path) -> None:
    """Every `.py` file below a directory is read, and other files are not."""
    (tmp_path / "pkg").mkdir()
    write(tmp_path / "pkg", FAILING["for"], "a.py")
    write(tmp_path / "pkg", FAILING["for"], "b.txt")

    violations = check([tmp_path])

    assert [violation.path.name for violation in violations] == ["a.py"]


def test_a_file_that_does_not_parse_is_a_violation(tmp_path: Path) -> None:
    """A syntax error never passes silently."""
    (violation,) = check([write(tmp_path, "def node(:\n")])

    assert violation.function == "<module>"
    assert violation.message.startswith("cannot parse: ")


def test_main_exits_1_and_prints_each_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command fails on a violation and names it."""
    path = write(tmp_path, FAILING["two-calls"])

    assert main([str(path)]) == 1
    assert capsys.readouterr().out == f"{path}:4: node: second interrupt() call\n"


def test_main_exits_0_on_clean_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command passes and prints nothing on clean code."""
    assert main([str(write(tmp_path, ONE_CALL))]) == 0
    assert not capsys.readouterr().out


@pytest.mark.parametrize("arguments", [[], ["missing.py"]], ids=["none", "missing"])
def test_main_exits_2_on_a_usage_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    """No path, or a path that does not exist, is a usage error."""
    monkeypatch.chdir(tmp_path)

    assert main(arguments) == 2  # ruff: ignore[magic-value-comparison]
    assert "usage: python -m hco_core.lint.interrupts" in capsys.readouterr().err


def test_the_hco_core_package_passes() -> None:
    """The rule holds on the package that CI checks."""
    assert check([Path(review.__file__).parent.parent]) == []


def test_the_module_command_exits_1_on_a_violation(tmp_path: Path) -> None:
    """`python -m hco_core.lint.interrupts`, as CI runs it, fails the build."""
    path = write(tmp_path, FAILING["while"])

    done = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-m", "hco_core.lint.interrupts", str(path)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert done.returncode == 1
    assert done.stdout == f"{path}:4: node: interrupt() inside while\n"
