"""Fingerprints that identify one change across many devices."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from hier_config.models import Dump, DumpLine

from hco_core.engine.errors import engine_errors


@dataclass
class _Node:
    line: DumpLine
    children: list["_Node"] = field(default_factory=list["_Node"])

    def canonical(self) -> str:
        """Return this subtree as compact JSON with siblings in a fixed order."""
        line = json.dumps(
            [
                self.line.text,
                sorted(self.line.tags),
                sorted(self.line.comments),
                self.line.new_in_config,
            ],
            separators=(",", ":"),
        )
        children = ",".join(sorted(child.canonical() for child in self.children))
        return f"[{line},[{children}]]"


def change_fingerprint(dump: Mapping[str, object]) -> str:
    """Return the sha256 hex digest of the canonical Dump JSON.

    Sibling order does not change the digest, so two devices that need the
    same change share one fingerprint. A malformed dump raises
    `InvalidConfigError`.
    """
    with engine_errors():
        lines = Dump.model_validate(dump).lines
    root = _Node(
        DumpLine(
            depth=0,
            text="",
            tags=frozenset(),
            comments=frozenset(),
            new_in_config=False,
        )
    )
    sections = [root]
    for line in lines:
        while sections[-1].line.depth >= line.depth:
            sections.pop()
        node = _Node(line)
        sections[-1].children.append(node)
        sections.append(node)
    return hashlib.sha256(root.canonical().encode()).hexdigest()
