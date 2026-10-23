"""The registry maps adapter names to built-in factories."""

import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from hco_core.adapters import registry
from hco_core.adapters.errors import AdapterConfigError
from hco_core.adapters.sot.nautobot import NautobotSourceOfTruth
from hco_core.ports import (
    ConnectionDriver,
    NotificationChannel,
    SecretsProvider,
    SourceOfTruth,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
KEY = b"k" * 32
TOKEN = SecretStr("registry-token-marker")
NAUTOBOT = {"url": "https://nautobot.example"}


def test_each_built_in_name_builds_its_adapter() -> None:
    """Each name gives an adapter that satisfies its port."""
    sot = registry.source_of_truth("yaml", {"path": str(FIXTURES / "inventory.yaml")})
    assert isinstance(sot, SourceOfTruth)
    options = {"transcripts_dir": str(FIXTURES / "transcripts")}
    assert isinstance(
        registry.connection("mock", options, token_key=KEY), ConnectionDriver
    )
    pytest.importorskip("scrapli")
    assert registry.connection("scrapli", {}, token_key=KEY).name == "scrapli"
    assert isinstance(registry.secrets("env", {}), SecretsProvider)
    assert isinstance(registry.notification_channel("log", {}), NotificationChannel)


def test_an_unknown_name_lists_the_known_names() -> None:
    """The error names the kind and every known name."""
    with pytest.raises(AdapterConfigError, match=r"sot.*'mod\.path'.*nautobot, yaml"):
        registry.source_of_truth("mod.path", {})
    with pytest.raises(AdapterConfigError, match=r"connection.*mock, scrapli"):
        registry.connection("netmiko", {}, token_key=KEY)
    with pytest.raises(AdapterConfigError, match=r"secrets.*env"):
        registry.secrets("vault", {})
    with pytest.raises(AdapterConfigError, match=r"notify.*log"):
        registry.notification_channel("slack", {})


def test_scrapli_names_its_extra_when_the_package_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the `scrapli` extra, the error says how to install it."""
    monkeypatch.setitem(sys.modules, "scrapli", None)
    monkeypatch.delitem(sys.modules, "hco_core.adapters.conn.scrapli", raising=False)

    with pytest.raises(AdapterConfigError, match=r"hco-core\[scrapli\]"):
        registry.connection("scrapli", {}, token_key=KEY)


def test_a_missing_option_is_named() -> None:
    """A factory names the option that it needs."""
    with pytest.raises(AdapterConfigError, match=r"yaml.*path"):
        registry.source_of_truth("yaml", {})


def test_nautobot_takes_its_options_and_the_token() -> None:
    """The token is a separate argument, so it is never a plain option."""
    options = {
        **NAUTOBOT,
        "verify_ssl": "False",
        "timeout": "5",
        "redundancy_tag_prefix": "pair:",
    }
    sot = registry.source_of_truth("nautobot", options, token=TOKEN)
    assert isinstance(sot, NautobotSourceOfTruth)
    assert repr(sot) == "NautobotSourceOfTruth(url='https://nautobot.example')"


@pytest.mark.parametrize(
    ("options", "token", "problem"),
    [
        (NAUTOBOT, None, "HCO_NAUTOBOT_TOKEN"),
        ({}, TOKEN, "HCO_NAUTOBOT_URL"),
        ({**NAUTOBOT, "verify_ssl": "maybe"}, TOKEN, "HCO_NAUTOBOT_VERIFY_SSL"),
        ({**NAUTOBOT, "timeout": "soon"}, TOKEN, "HCO_NAUTOBOT_TIMEOUT"),
        ({**NAUTOBOT, "timeout": "0"}, TOKEN, "HCO_NAUTOBOT_TIMEOUT"),
        ({**NAUTOBOT, "timeout": "nan"}, TOKEN, "HCO_NAUTOBOT_TIMEOUT"),
    ],
)
def test_nautobot_names_the_setting_of_a_bad_option(
    options: dict[str, str], token: SecretStr | None, problem: str
) -> None:
    """The error names the variable to set, never the token."""
    with pytest.raises(AdapterConfigError, match=problem) as error:
        registry.source_of_truth("nautobot", options, token=token)
    assert "registry-token-marker" not in str(error.value)
