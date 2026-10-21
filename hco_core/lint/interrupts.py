"""Fail on a graph node that can call `interrupt()` more than one time per run.

A resumed node runs again from the top, so its one `interrupt()` must be
unconditional, and each call above it must be idempotent. This rule finds a
second `interrupt()` in one function, and an `interrupt()` inside a branch, a
loop, a `try`, a `with`, a `match`, a boolean operator, a comprehension, or a
lambda. Run it as `python -m hco_core.lint.interrupts <paths>`.
"""

import ast
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

INTERRUPT: Final = "interrupt"
USAGE: Final = "usage: python -m hco_core.lint.interrupts <path> [<path> ...]\n"
EXIT_VIOLATION: Final = 1
EXIT_USAGE: Final = 2

_CONDITIONAL: Final[dict[type[ast.AST], str]] = {
    ast.If: "if",
    ast.IfExp: "a conditional expression",
    ast.For: "for",
    ast.AsyncFor: "for",
    ast.While: "while",
    ast.Try: "try",
    ast.TryStar: "try",
    ast.With: "with",
    ast.AsyncWith: "with",
    ast.Match: "match",
    ast.BoolOp: "a boolean operator",
    ast.ListComp: "a comprehension",
    ast.SetComp: "a comprehension",
    ast.DictComp: "a comprehension",
    ast.GeneratorExp: "a comprehension",
    ast.Lambda: "a lambda",
}
_FUNCTIONS: Final = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES: Final = (*_FUNCTIONS, ast.ClassDef)
_MODULE: Final = "<module>"


@dataclass(frozen=True, slots=True)
class Violation:
    """One `interrupt()` call that breaks the rule."""

    path: Path
    line: int
    function: str
    message: str

    def __str__(self) -> str:
        """Return the violation as `path:line: function: message`."""
        return f"{self.path}:{self.line}: {self.function}: {self.message}"


def check(paths: Iterable[Path]) -> list[Violation]:
    """Return the violations in each file, and in each `.py` file below each directory."""
    return [
        violation
        for path in paths
        for source in _python_files(path)
        for violation in _check_file(source)
    ]


def _python_files(path: Path) -> list[Path]:
    return sorted(path.rglob("*.py")) if path.is_dir() else [path]


def _check_file(path: Path) -> list[Violation]:
    try:
        module = ast.parse(path.read_text(encoding="utf8"), str(path))
    except SyntaxError as error:
        return [
            Violation(path, error.lineno or 1, _MODULE, f"cannot parse: {error.msg}")
        ]
    names = _interrupt_names(module)
    violations: list[Violation] = []
    for scope in [module, *_scopes(module)]:
        name = scope.name if isinstance(scope, _SCOPES) else _MODULE
        in_function = isinstance(scope, _FUNCTIONS)
        for index, (call, enclosing) in enumerate(_calls(scope, names, ())):
            message = _message(index, enclosing, in_function=in_function)
            if message:
                violations.append(Violation(path, call.lineno, name, message))
    return sorted(violations, key=lambda violation: violation.line)


def _message(index: int, enclosing: tuple[str, ...], *, in_function: bool) -> str:
    if enclosing:
        return f"{INTERRUPT}() inside {enclosing[-1]}"
    if not in_function:
        return f"{INTERRUPT}() outside a function"
    if index:
        return f"second {INTERRUPT}() call"
    return ""


def _interrupt_names(module: ast.Module) -> frozenset[str]:
    aliases = {
        alias.asname
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == INTERRUPT and alias.asname
    }
    return frozenset({INTERRUPT, *aliases})


def _scopes(
    module: ast.Module,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    for node in ast.walk(module):
        if isinstance(node, _SCOPES):
            yield node


def _calls(
    node: ast.AST, names: frozenset[str], enclosing: tuple[str, ...]
) -> Iterator[tuple[ast.Call, tuple[str, ...]]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPES):
            continue
        if isinstance(child, ast.Call) and _is_interrupt(child, names):
            yield child, enclosing
        kind = _CONDITIONAL.get(type(child))
        yield from _calls(child, names, (*enclosing, kind) if kind else enclosing)


def _is_interrupt(call: ast.Call, names: frozenset[str]) -> bool:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id in names
    return isinstance(function, ast.Attribute) and function.attr == INTERRUPT


def main(arguments: Sequence[str] | None = None) -> int:
    """Check the paths, print each violation, and return the exit code."""
    given = sys.argv[1:] if arguments is None else arguments
    paths = [Path(argument) for argument in given]
    missing = [str(path) for path in paths if not path.exists()]
    if not paths or missing:
        problem = f"no such path: {', '.join(missing)}\n" if missing else ""
        sys.stderr.write(f"{problem}{USAGE}")
        return EXIT_USAGE
    violations = check(paths)
    sys.stdout.writelines(f"{violation}\n" for violation in violations)
    return EXIT_VIOLATION if violations else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
