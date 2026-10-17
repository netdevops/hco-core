"""Map adapter names to built-in factories.

The registry holds built-in factories only. It never imports a module or an
object from a name, so a configuration value cannot load code.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.errors import AdapterConfigError
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.ports import ConnectionDriver, SecretsProvider, SourceOfTruth

type Options = Mapping[str, str]

_SOURCES_OF_TRUTH: Mapping[str, Callable[[Options], SourceOfTruth]] = MappingProxyType(
    {"yaml": lambda options: YamlSourceOfTruth(Path(_option(options, "yaml", "path")))}
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


def source_of_truth(name: str, options: Options) -> SourceOfTruth:
    """Return the source of truth with the name. `yaml` needs the `path` option."""
    return _factory("sot", _SOURCES_OF_TRUTH, name)(options)


def connection(name: str, options: Options, *, token_key: bytes) -> ConnectionDriver:
    """Return the connection driver with the name.

    `mock` needs the `transcripts_dir` option. The key verifies plan tokens.
    """
    return _factory("connection", _CONNECTIONS, name)(options, token_key)


def secrets(name: str, options: Options) -> SecretsProvider:
    """Return the secrets provider with the name. `env` needs no option."""
    return _factory("secrets", _SECRETS, name)(options)


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
