"""Fingerprints that identify one change across many devices."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from hier_config.models import Dump, DumpLine
from pydantic import JsonValue

from hco_core.engine.errors import engine_errors


@dataclass
class _Node:
    line: DumpLine
    children: list["_Node"] = field(default_factory=list["_Node"])

    def canonical(self) -> JsonValue:
        """Return this subtree as JSON with siblings in a fixed order."""
        tags: list[JsonValue] = [*sorted(self.line.tags)]
        comments: list[JsonValue] = [*sorted(self.line.comments)]
        children: list[JsonValue] = sorted(
            (child.canonical() for child in self.children), key=_canonical_json
        )
        return {
            "text": self.line.text,
            "tags": tags,
            "comments": comments,
            "new_in_config": self.line.new_in_config,
            "children": children,
        }


def change_fingerprint(dump: Mapping[str, object]) -> str:
    """Return the sha256 hex digest of the canonical Dump JSON.

    Sibling order does not change the digest, so two devices that need the
    same change share one fingerprint. A malformed dump raises
    `InvalidConfigError`.
    """
    with engine_errors():
        lines = Dump.model_validate(dump).lines
    roots: list[_Node] = []
    sections: list[_Node] = []
    for line in lines:
        while sections and sections[-1].line.depth >= line.depth:
            sections.pop()
        node = _Node(line)
        (sections[-1].children if sections else roots).append(node)
        sections.append(node)
    tree = sorted((root.canonical() for root in roots), key=_canonical_json)
    return hashlib.sha256(_canonical_json(tree).encode()).hexdigest()


def _canonical_json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
