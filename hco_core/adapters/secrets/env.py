"""Credentials from environment variables, for development and tests."""

import os
import re
from collections.abc import Mapping

from pydantic import SecretStr

from hco_core.adapters.errors import AdapterConfigError, SecretNotFoundError
from hco_core.domain.secrets import DeviceCredentials, SecretRef

_NOT_NAME_CHARACTERS = re.compile(r"[^A-Z0-9]")


class EnvSecretsProvider:
    """Resolve an `env` reference from `HCO_SECRET_<KEY>_<FIELD>` variables.

    `<KEY>` is the reference key in upper case, with each character other than
    a letter or a digit changed to `_`. The fields are `USERNAME`, `PASSWORD`,
    `ENABLE`, and `API_KEY`. `ENABLE` is optional, and a blank value is the same
    as no value.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        """Read from the mapping, or from `os.environ` when it is None."""
        self._environ = os.environ if environ is None else environ

    async def resolve(self, ref: SecretRef) -> DeviceCredentials:
        """Return the username, the password, and the optional enable secret."""
        prefix = _prefix(ref)
        enable = self._environ.get(f"{prefix}_ENABLE")
        return DeviceCredentials(
            username=self._value(f"{prefix}_USERNAME"),
            password=SecretStr(self._value(f"{prefix}_PASSWORD")),
            enable=SecretStr(enable) if enable else None,
        )

    async def resolve_api_key(self, ref: SecretRef) -> SecretStr:
        """Return the API key of the reference."""
        return SecretStr(self._value(f"{_prefix(ref)}_API_KEY"))

    def _value(self, variable: str) -> str:
        value = self._environ.get(variable)
        if value is None:
            message = f"the environment variable {variable} is not set"
            raise SecretNotFoundError(message)
        return value


def _prefix(ref: SecretRef) -> str:
    if ref.provider != "env" or not ref.key:
        message = f"the env provider cannot resolve a {ref.provider!r} reference with key {ref.key!r}"
        raise AdapterConfigError(message)
    return f"HCO_SECRET_{_NOT_NAME_CHARACTERS.sub('_', ref.key.upper())}"
