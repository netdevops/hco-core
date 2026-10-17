"""The single gate that every device write must pass.

`validate_plan` runs checks 1 to 9 of the remediation authoring spec and signs
only a plan that passes all of them. `DeviceSession.apply()` must refuse a plan
whose token does not verify. When in doubt, a check blocks.
"""

import hashlib
import hmac
import json
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager

from hier_config import HConfig, HierConfigError, WorkflowRemediation
from hier_config.registry import get_hconfig_driver

from hco_core.domain.validation import ValidationOutcome
from hco_core.engine.errors import engine_errors
from hco_core.engine.remediation import load_dump

type _Path = tuple[str, ...]

MIN_KEY_BYTES = 32
"""The digest size of SHA-256. RFC 2104 discourages a shorter HMAC key."""
_CANONICAL_TAG = "hco-core/validation/v1"
_DESTRUCTIVE = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*(do\s+)?relo",
        r"^\s*(do\s+)?wri?t?e?\s+era",
        r"^\s*(do\s+)?crypto\s+key\s+zeroiz",
        r"^\s*(do\s+)?forma",
        r"^\s*(do\s+)?request\s+system\s+(reboo|halt|zeroiz|power-off)",
    )
)
_ACCESS_LISTS = ("ip access-list ", "ipv4 access-list ", "ipv6 access-list ")


class _BlockedError(Exception):
    """A check failed. Only `validate_plan` catches it."""

    def __init__(self, check: int, problem: str) -> None:
        super().__init__(problem)
        self.check = check
        self.problem = problem


def validate_plan(  # ruff: ignore[too-many-arguments]
    plan: Sequence[str],
    running: Mapping[str, object],
    intended: Mapping[str, object],
    *,
    platform_name: str,
    forbidden: Sequence[re.Pattern[str]],
    key: bytes,
) -> ValidationOutcome:
    """Run checks 1 to 9 on the plan and sign it if every check passes.

    The outcome of a blocked plan names the first check that failed. Checks 4
    to 6 run on the plan without its scaffolding pairs, and check 7 validates
    each pair. A key shorter than `MIN_KEY_BYTES` raises `ValueError`. A
    malformed dump or an unknown platform name raises an `EngineError`.
    """
    _require_key(key)
    lines = tuple(plan)
    with engine_errors():
        running_config = load_dump(platform_name, running)
        intended_config = load_dump(platform_name, intended)
        negation = get_hconfig_driver(platform_name).negation_prefix
    try:
        scaffolding = _run_checks(
            lines, running_config, intended_config, negation, forbidden
        )
    except _BlockedError as blocked:
        return ValidationOutcome(
            failed_check=blocked.check, problems=(blocked.problem,)
        )
    return ValidationOutcome(
        commands_requiring_review=tuple(
            line for line in lines if line.strip().startswith(negation)
        ),
        scaffolding=scaffolding,
        token=_sign(lines, key),
    )


def verify_token(plan: Sequence[str], token: str, key: bytes) -> bool:
    """Return True if the token is the signature of this exact plan.

    A key shorter than `MIN_KEY_BYTES` raises `ValueError`.
    """
    _require_key(key)
    return hmac.compare_digest(_sign(tuple(plan), key).encode(), token.encode())


def _require_key(key: bytes) -> None:
    if len(key) < MIN_KEY_BYTES:
        message = f"the token key must have at least {MIN_KEY_BYTES} bytes"
        raise ValueError(message)


def _sign(lines: tuple[str, ...], key: bytes) -> str:
    canonical = json.dumps([_CANONICAL_TAG, list(lines)], separators=(",", ":"))
    return hmac.new(key, canonical.encode("ascii"), hashlib.sha256).hexdigest()


@contextmanager
def _at_check(check: int) -> Generator[None]:
    try:
        yield
    except (HierConfigError, ValueError, RecursionError) as error:
        raise _BlockedError(check, f"hier_config refused the plan: {error}") from error


def _run_checks(
    lines: tuple[str, ...],
    running: HConfig,
    intended: HConfig,
    negation: str,
    forbidden: Sequence[re.Pattern[str]],
) -> tuple[str, ...]:
    """Return the scaffolding lines of a plan that passes checks 1 to 8.

    Raises:
        _BlockedError: At the first check that fails.

    """
    _check_shape(lines)
    with _at_check(2):
        paths = _parsed_paths(running, lines)
    _check_destructive(lines)
    pairs = _scaffolding_pairs(paths, negation)
    paired = {index for pair in pairs for index in _subtree(paths, *pair)}
    kept = [index for index in range(len(lines)) if index not in paired]
    with _at_check(4):
        effective = HConfig.from_lines(running.driver, [lines[i] for i in kept])
        future, report = running.future_with_report(effective)
    if report.unresolved_negations:
        texts = ", ".join(child.text for child in report.unresolved_negations)
        raise _BlockedError(4, f"negations that match nothing in running: {texts}")
    with _at_check(5):
        canonical = running.future(
            WorkflowRemediation(running, intended).remediation_config
        )
    _check_convergence(set(canonical.to_lines()), set(future.to_lines()))
    with _at_check(6):
        residual = WorkflowRemediation(future, intended).remediation_config.to_lines()
    if residual:
        raise _BlockedError(6, f"residual remediation after the plan: {residual}")
    running_children = _children_by_parent(
        tuple(child.path()) for child in running.all_children()
    )
    plan_children = _children_by_parent(paths[index] for index in kept)
    for add, removal in pairs:
        _check_scaffolding(
            paths[add], paths[removal], running_children, plan_children, negation
        )
    _check_forbidden(lines, forbidden)
    return tuple(lines[index] for index in sorted({i for pair in pairs for i in pair}))


def _check_shape(lines: tuple[str, ...]) -> None:
    if not lines:
        raise _BlockedError(1, "the plan is empty")
    for number, line in enumerate(lines, start=1):
        if "\t" in line:
            raise _BlockedError(1, f"line {number} contains a tab")
        if not (line.isascii() and line.isprintable()):
            raise _BlockedError(1, f"line {number} is not printable ASCII")
        if not line.strip():
            raise _BlockedError(1, f"line {number} is blank")


def _parsed_paths(running: HConfig, lines: tuple[str, ...]) -> tuple[_Path, ...]:
    plan_config = HConfig.from_lines(running.driver, lines)
    paths = tuple(tuple(child.path()) for child in plan_config.all_children())
    for index, line in enumerate(lines):
        if index >= len(paths) or paths[index][-1] != line.strip():
            message = f"the driver does not read line {index + 1} as written: {line!r}"
            raise _BlockedError(2, message)
    return paths


def _check_destructive(lines: tuple[str, ...]) -> None:
    for line in lines:
        if any(pattern.search(line) for pattern in _DESTRUCTIVE):
            raise _BlockedError(3, f"destructive: {line!r}")


def _scaffolding_pairs(
    paths: tuple[_Path, ...], negation: str
) -> tuple[tuple[int, int], ...]:
    """Pair each negation with the latest earlier added line that it may remove.

    The match is loose on purpose: `no ip` pairs with `ip route ...`, so that
    check 7 can refuse the pair.
    """
    adds_by_prefix: dict[tuple[_Path, str], list[int]] = {}
    paired: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for index, path in enumerate(paths):
        parent, text = path[:-1], path[-1]
        if not text.startswith(negation):
            words = text.split(" ")
            for count in range(1, len(words) + 1):
                prefix = " ".join(words[:count])
                adds_by_prefix.setdefault((parent, prefix), []).append(index)
            continue
        target = text.removeprefix(negation)
        candidates = adds_by_prefix.get((parent, target), [])
        while candidates and candidates[-1] in paired:
            candidates.pop()
        if candidates:
            add = candidates.pop()
            paired.add(add)
            pairs.append((add, index))
    return tuple(pairs)


def _subtree(paths: tuple[_Path, ...], *indexes: int) -> Iterable[int]:
    for index in indexes:
        root = paths[index]
        end = index + 1
        while end < len(paths) and paths[end][: len(root)] == root:
            end += 1
        yield from range(index, end)


def _children_by_parent(paths: Iterable[_Path]) -> dict[_Path, list[str]]:
    children: dict[_Path, list[str]] = {}
    for path in paths:
        children.setdefault(path[:-1], []).append(path[-1])
    return children


def _check_convergence(canonical: set[str], future: set[str]) -> None:
    missing, unwanted = sorted(canonical - future), sorted(future - canonical)
    if missing or unwanted:
        message = f"the plan does not converge: missing {missing}, unwanted {unwanted}"
        raise _BlockedError(5, message)


def _check_scaffolding(
    add: _Path,
    removal: _Path,
    running_children: dict[_Path, list[str]],
    plan_children: dict[_Path, list[str]],
    negation: str,
) -> None:
    parent, added = add[:-1], add[-1]
    target = removal[-1].removeprefix(negation)
    running_texts = running_children.get(parent, [])
    if added == target:
        removes_running = added in running_texts
    elif target.isdigit():
        removes_running = any(text.split()[0] == target for text in running_texts)
    else:
        message = f"scaffolding removal {removal[-1]!r} does not name {added!r}"
        raise _BlockedError(7, message)
    if removes_running:
        message = f"scaffolding removal {removal[-1]!r} also removes a running line"
        raise _BlockedError(7, message)
    sequence, _, command = added.lower().partition(" ")
    if sequence == "permit" or command.startswith("permit "):
        plan_texts = plan_children.get(parent, [])
        _check_permit(sequence, parent, running_texts, plan_texts, negation)


def _check_permit(
    sequence: str,
    parent: _Path,
    running_texts: list[str],
    plan_texts: list[str],
    negation: str,
) -> None:
    others = [text.split()[0] for text in (*running_texts, *plan_texts)]
    if not (
        sequence.isdigit()
        and len(parent) == 1
        and parent[0].startswith(_ACCESS_LISTS)
        and any(text.startswith(negation) for text in plan_texts)
        and all(int(sequence) < int(other) for other in others if other.isdigit())
    ):
        message = (
            "a permit is scaffolding only in an access list that the plan "
            f"resequences, below every other entry: {parent}"
        )
        raise _BlockedError(7, message)


def _check_forbidden(
    lines: tuple[str, ...], forbidden: Sequence[re.Pattern[str]]
) -> None:
    for line in lines:
        if any(pattern.search(line) for pattern in forbidden):
            raise _BlockedError(8, f"forbidden: {line!r}")
