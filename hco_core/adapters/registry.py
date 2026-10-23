"""Map adapter names to built-in factories.

The registry holds built-in factories only. It never imports a module or an
object from a name, so a configuration value cannot load code.
"""

import math
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType

from pydantic import SecretStr

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.errors import AdapterConfigError
from hco_core.adapters.notify.log import LogChannel
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.adapters.sot.nautobot import (
    DEFAULT_REDUNDANCY_TAG_PREFIX,
    NautobotSourceOfTruth,
)
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.ports import (
    ConnectionDriver,
    NotificationChannel,
    SecretsProvider,
    SourceOfTruth,
)

type Options = Mapping[str, str]

_BOOLEANS = MappingProxyType({"true": True, "false": False})


def _nautobot(options: Options, token: SecretStr | None) -> SourceOfTruth:
    """Build the Nautobot adapter. Each error names the `HCO_NAUTOBOT_` variable.

    Raises:
        AdapterConfigError: If the URL or the token is missing, or an option
            is not valid.

    """
    if token is None:
        message = "the nautobot adapter needs a token; set HCO_NAUTOBOT_TOKEN"
        raise AdapterConfigError(message)
    if "url" not in options:
        message = "the nautobot adapter needs the option 'url'; set HCO_NAUTOBOT_URL"
        raise AdapterConfigError(message)
    verify_ssl = _BOOLEANS.get(options.get("verify_ssl", "true").lower())
    if verify_ssl is None:
        message = "HCO_NAUTOBOT_VERIFY_SSL must be true or false"
        raise AdapterConfigError(message)
    try:
        timeout = float(options.get("timeout", "30"))
    except ValueError:
        timeout = math.nan
    if not (math.isfinite(timeout) and timeout > 0):
        message = "HCO_NAUTOBOT_TIMEOUT must be a number of seconds above 0"
        raise AdapterConfigError(message)
    return NautobotSourceOfTruth(
        options["url"],
        token,
        verify_ssl=verify_ssl,
        timeout=timeout,
        redundancy_tag_prefix=options.get(
            "redundancy_tag_prefix", DEFAULT_REDUNDANCY_TAG_PREFIX
        ),
    )


_SOURCES_OF_TRUTH: Mapping[
    str, Callable[[Options, SecretStr | None], SourceOfTruth]
] = MappingProxyType(
    {
        "nautobot": _nautobot,
        "yaml": lambda options, _: YamlSourceOfTruth(
            Path(_option(options, "yaml", "path"))
        ),
    }
)
_CONNECTIONS: Mapping[str, Callable[[Options, bytes], ConnectionDriver]] = (
    MappingProxyType(
        {
            "mock": lambda options, key: MockConnectionDriver(
                Path(_option(options, "mock", "transcripts_dir")), token_key=key
            )
        }
    )
)
_SECRETS: Mapping[str, Callable[[Options], SecretsProvider]] = MappingProxyType(
    {"env": lambda _: EnvSecretsProvider()}
)
_NOTIFY: Mapping[str, Callable[[Options], NotificationChannel]] = MappingProxyType(
    {"log": lambda _: LogChannel()}
)


def source_of_truth(
    name: str, options: Options, *, token: SecretStr | None = None
) -> SourceOfTruth:
    """Return the source of truth with the name.

    `yaml` needs the `path` option. `nautobot` needs the `url` option and the
    token, and takes `verify_ssl`, `timeout`, and `redundancy_tag_prefix`.
    """
    return _factory("sot", _SOURCES_OF_TRUTH, name)(options, token)


def connection(name: str, options: Options, *, token_key: bytes) -> ConnectionDriver:
    """Return the connection driver with the name.

    `mock` needs the `transcripts_dir` option. The key verifies plan tokens.
    """
    return _factory("connection", _CONNECTIONS, name)(options, token_key)


def secrets(name: str, options: Options) -> SecretsProvider:
    """Return the secrets provider with the name. `env` needs no option."""
    return _factory("secrets", _SECRETS, name)(options)


def notification_channel(name: str, options: Options) -> NotificationChannel:
    """Return the notification channel with the name. `log` needs no option."""
    return _factory("notify", _NOTIFY, name)(options)


def _factory[F](kind: str, factories: Mapping[str, F], name: str) -> F:
    if name not in factories:
        message = f"unknown {kind} adapter {name!r}; known names: {', '.join(sorted(factories))}"
        raise AdapterConfigError(message)
    return factories[name]


def _option(options: Options, adapter: str, key: str) -> str:
    if key not in options:
        message = f"the {adapter} adapter needs the option {key!r}"
        raise AdapterConfigError(message)
    return options[key]
