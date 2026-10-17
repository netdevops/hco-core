"""Errors the engine raises in place of `hier_config` errors."""

from collections.abc import Generator
from contextlib import contextmanager

from hier_config import DriverNotFoundError, HierConfigError, IncompatibleDriverError
from pydantic import ValidationError


class EngineError(Exception):
    """Base class of every error the engine raises."""


class UnknownPlatformError(EngineError):
    """No driver is registered for the platform name."""


class InvalidConfigError(EngineError):
    """A config text, a dump, or a tag rule is malformed."""


class DriverMismatchError(EngineError):
    """The running and intended configs use different drivers."""


@contextmanager
def engine_errors() -> Generator[None]:
    """Raise the matching engine error for a `hier_config` or schema error.

    Raises:
        UnknownPlatformError: For a `DriverNotFoundError`.
        DriverMismatchError: For an `IncompatibleDriverError`.
        InvalidConfigError: For any other `hier_config` or schema error.

    """
    try:
        yield
    except DriverNotFoundError as error:
        raise UnknownPlatformError(str(error)) from error
    except IncompatibleDriverError as error:
        raise DriverMismatchError(str(error)) from error
    except (HierConfigError, ValidationError) as error:
        raise InvalidConfigError(str(error)) from error
