"""A change fingerprint identifies one change across many devices."""

from __future__ import annotations

import re

import pytest

from hco_core.engine.errors import InvalidConfigError
from hco_core.engine.fingerprints import change_fingerprint
from hco_core.engine.remediation import parse

IOS = "CISCO_IOS"
TREE = "interface Gi1\n description a\n shutdown\ninterface Gi2\n mtu 9000\n"
REORDERED = "interface Gi2\n mtu 9000\ninterface Gi1\n shutdown\n description a\n"


def test_line_order_does_not_change_the_fingerprint() -> None:
    """Two dumps of one tree share one sha256 hex fingerprint."""
    first, second = parse(IOS, TREE), parse(IOS, REORDERED)
    assert first != second
    assert change_fingerprint(first) == change_fingerprint(second)
    assert re.fullmatch(r"[0-9a-f]{64}", change_fingerprint(first))


@pytest.mark.parametrize(
    "other",
    [
        "interface Gi1\n description b\n",
        "interface Gi1\ninterface Gi2\n description a\n",
    ],
)
def test_a_different_change_alters_the_fingerprint(other: str) -> None:
    """Changed text, or the same line in another section, is another change."""
    first = parse(IOS, "interface Gi1\n description a\ninterface Gi2\n")
    assert change_fingerprint(first) != change_fingerprint(parse(IOS, other))


def test_malformed_dump_is_invalid_config() -> None:
    """A dump that fails the schema is an engine error."""
    with pytest.raises(InvalidConfigError):
        change_fingerprint({"lines": "x"})
