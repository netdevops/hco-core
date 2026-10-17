"""The registry maps adapter names to built-in factories."""

from pathlib import Path

import pytest

from hco_core.adapters import registry
from hco_core.adapters.errors import AdapterConfigError
from hco_core.ports import ConnectionDriver, SecretsProvider, SourceOfTruth

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
KEY = b"k" * 32


def test_each_built_in_name_builds_its_adapter() -> None:
    """Each name gives an adapter that satisfies its port."""
    sot = registry.source_of_truth("yaml", {"path": str(FIXTURES / "inventory.yaml")})
    assert isinstance(sot, SourceOfTruth)
    options = {"transcripts_dir": str(FIXTURES / "transcripts")}
    assert isinstance(
        registry.connection("mock", options, token_key=KEY), ConnectionDriver
    )
    assert isinstance(registry.secrets("env", {}), SecretsProvider)


def test_an_unknown_name_lists_the_known_names() -> None:
    """The error names the kind and every known name."""
    with pytest.raises(AdapterConfigError, match=r"sot.*'mod\.path'.*yaml"):
        registry.source_of_truth("mod.path", {})
    with pytest.raises(AdapterConfigError, match=r"connection.*mock"):
        registry.connection("scrapli", {}, token_key=KEY)
    with pytest.raises(AdapterConfigError, match=r"secrets.*env"):
        registry.secrets("vault", {})


def test_a_missing_option_is_named() -> None:
    """A factory names the option that it needs."""
    with pytest.raises(AdapterConfigError, match=r"yaml.*path"):
        registry.source_of_truth("yaml", {})
