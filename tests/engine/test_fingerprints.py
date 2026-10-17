"""A change fingerprint identifies one change across many devices."""

from __future__ import annotations

import pytest

from hco_core.engine.errors import InvalidConfigError
from hco_core.engine.fingerprints import change_fingerprint
from hco_core.engine.remediation import parse

SHA256_HEX_LENGTH = 64


def test_fingerprint_is_a_sha256_hex_digest() -> None:
    """The fingerprint fits the String(64) column."""
    fingerprint = change_fingerprint(parse("CISCO_IOS", "hostname a\n"))
    assert len(fingerprint) == SHA256_HEX_LENGTH
    assert int(fingerprint, 16) >= 0


def test_line_order_does_not_change_the_fingerprint() -> None:
    """Two dumps of the same tree share one fingerprint."""
    first = parse(
        "CISCO_IOS",
        "interface Gi1\n description a\n shutdown\ninterface Gi2\n mtu 9000\n",
    )
    second = parse(
        "CISCO_IOS",
        "interface Gi2\n mtu 9000\ninterface Gi1\n shutdown\n description a\n",
    )
    assert first != second
    assert change_fingerprint(first) == change_fingerprint(second)


def test_a_text_change_alters_the_fingerprint() -> None:
    """A different change has a different fingerprint."""
    first = parse("CISCO_IOS", "interface Gi1\n description a\n")
    second = parse("CISCO_IOS", "interface Gi1\n description b\n")
    assert change_fingerprint(first) != change_fingerprint(second)


def test_a_moved_line_alters_the_fingerprint() -> None:
    """The same line in a different section is a different change."""
    first = parse("CISCO_IOS", "interface Gi1\n shutdown\ninterface Gi2\n")
    second = parse("CISCO_IOS", "interface Gi1\ninterface Gi2\n shutdown\n")
    assert change_fingerprint(first) != change_fingerprint(second)


def test_malformed_dump_is_invalid_config() -> None:
    """A dump that fails the schema is an engine error."""
    with pytest.raises(InvalidConfigError):
        change_fingerprint({"lines": "x"})
