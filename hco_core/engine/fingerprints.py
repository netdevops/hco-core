"""Fingerprints that identify one change or one driver."""

import hashlib
import json
from collections.abc import Mapping

from hier_config.models import DumpLine
from hier_config.registry import get_hconfig_driver

from hco_core.engine.errors import engine_errors
from hco_core.engine.platforms import canonical_platform_name
from hco_core.engine.remediation import validated_dump

type _OpenSection = tuple[DumpLine | None, list[str]]

DRIVER_RULES_EXCLUDED = {"post_load_callbacks", "remediation_transform_callbacks"}
"""The `HConfigDriverRules` fields that hold functions, not data."""


def change_fingerprint(dump: Mapping[str, object]) -> str:
    """Return the sha256 hex digest of the canonical Dump JSON.

    Sibling order and the comments that `hier_config` writes do not change the
    digest, so two devices that need the same change share one fingerprint.
    A malformed dump raises `InvalidConfigError`.
    """
    with engine_errors():
        lines = validated_dump(dump).lines
    sections: list[_OpenSection] = [(None, [])]
    for line in lines:
        while len(sections) > line.depth:
            _close(sections)
        sections.append((line, []))
    while len(sections) > 1:
        _close(sections)
    _, roots = sections[0]
    return hashlib.sha256(_join(roots).encode()).hexdigest()


def driver_fingerprint(platform_name: str) -> str:
    """Return the sha256 hex digest of the platform name and its driver rules.

    The canonical JSON holds both, with each set sorted, so the digest does not
    depend on the hash seed of a process. It does not see the functions in
    `DRIVER_RULES_EXCLUDED`, or a driver class attribute outside `rules`, such as
    `negation_prefix`. An unknown platform raises `UnknownPlatformError`.
    """
    name = canonical_platform_name(platform_name)
    with engine_errors():
        rules = get_hconfig_driver(name).rules
    canonical = json.dumps(
        {"platform": name, "rules": rules.model_dump(exclude=DRIVER_RULES_EXCLUDED)},
        sort_keys=True,
        separators=(",", ":"),
        default=sorted,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _close(sections: list[_OpenSection]) -> None:
    line, children = sections.pop()
    if line is not None:
        fields = [line.text, sorted(line.tags), line.new_in_config]
        text = json.dumps(fields, separators=(",", ":"))
        sections[-1][1].append(f"[{text},{_join(children)}]")


def _join(subtrees: list[str]) -> str:
    return f"[{','.join(sorted(subtrees))}]"
