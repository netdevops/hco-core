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
