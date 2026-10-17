"""Fingerprints that identify one change across many devices."""

import hashlib
import json
from collections.abc import Mapping

from hier_config.models import DumpLine

from hco_core.engine.errors import engine_errors
from hco_core.engine.remediation import validated_dump

type _OpenSection = tuple[DumpLine | None, list[str]]


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


def _close(sections: list[_OpenSection]) -> None:
    line, children = sections.pop()
    if line is not None:
        fields = [line.text, sorted(line.tags), line.new_in_config]
        text = json.dumps(fields, separators=(",", ":"))
        sections[-1][1].append(f"[{text},{_join(children)}]")


def _join(subtrees: list[str]) -> str:
    return f"[{','.join(sorted(subtrees))}]"
