"""Column types and key factories of the hco-core schema."""

from typing import override

from sqlalchemy import Dialect, String, TypeDecorator
from ulid import ULID

from hco_core.engine.platforms import canonical_platform_name

ULID_LENGTH = 26


def new_id() -> str:
    """Return a new ULID as its 26-character text."""
    return str(ULID())


class PlatformName(TypeDecorator[str]):
    """Persist a `hier_config` platform by name, never by its positional value.

    A `Platform` member, its name, and a lower-case spelling all store the same
    upper-case name. A custom platform registered with `register_driver()` is
    stored the same way. An unknown platform raises `UnknownPlatformError`.
    """

    impl = String(64)
    cache_ok = True

    @override
    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """Return the canonical platform name to store."""
        del dialect
        return None if value is None else canonical_platform_name(value)

    @override
    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        """Return the stored platform name unchanged."""
        del dialect
        return value
