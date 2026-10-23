"""The env secrets provider reads credentials from environment variables."""

import asyncio

import pytest

from hco_core.adapters.errors import AdapterConfigError, SecretNotFoundError
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.domain.secrets import SecretPurpose, SecretRef
from hco_core.ports import SecretsProvider

PASSWORD = "s3cret-pass"  # ruff: ignore[hardcoded-password-string]
ENVIRON = {
    "HCO_SECRET_LAB_1_USERNAME": "admin",
    "HCO_SECRET_LAB_1_PASSWORD": PASSWORD,
    "HCO_SECRET_LAB_1_API_KEY": "key-1",
}


def test_resolve_reads_the_username_and_password() -> None:
    """The key is upper case, and each other character becomes an underscore."""
    provider = EnvSecretsProvider(ENVIRON)
    assert isinstance(provider, SecretsProvider)
    credentials = asyncio.run(provider.resolve(SecretRef(provider="env", key="lab-1")))
    assert credentials.username == "admin"
    assert credentials.password is not None
    assert credentials.password.get_secret_value() == PASSWORD
    for text in (repr(credentials), str(credentials), repr(provider)):
        assert PASSWORD not in text
    ref = SecretRef(provider="env", key="lab.1", purpose=SecretPurpose.API_KEY)
    assert asyncio.run(provider.resolve_api_key(ref)).get_secret_value() == "key-1"


def test_a_missing_variable_is_named_without_a_value() -> None:
    """The error names the variable, and never a value."""
    provider = EnvSecretsProvider({"HCO_SECRET_R1_USERNAME": "admin"})
    with pytest.raises(SecretNotFoundError, match="HCO_SECRET_R1_PASSWORD") as error:
        asyncio.run(provider.resolve(SecretRef(provider="env", key="r1")))
    assert "admin" not in str(error.value)


@pytest.mark.parametrize(
    "ref", [SecretRef(provider="vault", key="r1"), SecretRef(provider="env", key="")]
)
def test_a_reference_of_another_provider_is_refused(ref: SecretRef) -> None:
    """The env provider resolves only a non-empty env reference."""
    with pytest.raises(AdapterConfigError):
        asyncio.run(EnvSecretsProvider(ENVIRON).resolve(ref))


def test_the_process_environment_is_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a mapping, the provider reads `os.environ` when it resolves."""
    provider = EnvSecretsProvider()
    monkeypatch.setenv("HCO_SECRET_R2_USERNAME", "ops")
    monkeypatch.setenv("HCO_SECRET_R2_PASSWORD", PASSWORD)
    credentials = asyncio.run(provider.resolve(SecretRef(provider="env", key="r2")))
    assert credentials.username == "ops"


def test_resolve_reads_the_optional_enable_secret() -> None:
    """`HCO_SECRET_<KEY>_ENABLE` becomes the enable secret, hidden in every text."""
    enable = "en4ble-value"
    provider = EnvSecretsProvider({**ENVIRON, "HCO_SECRET_LAB_1_ENABLE": enable})
    credentials = asyncio.run(provider.resolve(SecretRef(provider="env", key="lab-1")))
    assert credentials.enable is not None
    assert credentials.enable.get_secret_value() == enable
    for text in (repr(credentials), str(credentials), repr(provider)):
        assert enable not in text


@pytest.mark.parametrize("enable", [None, ""])
def test_an_absent_or_blank_enable_secret_is_none(enable: str | None) -> None:
    """Without a value in `HCO_SECRET_<KEY>_ENABLE`, the enable secret is None."""
    environ = dict(ENVIRON)
    if enable is not None:
        environ["HCO_SECRET_LAB_1_ENABLE"] = enable
    provider = EnvSecretsProvider(environ)
    credentials = asyncio.run(provider.resolve(SecretRef(provider="env", key="lab-1")))
    assert credentials.enable is None
