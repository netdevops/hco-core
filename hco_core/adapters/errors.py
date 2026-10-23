"""The errors that every built-in adapter raises."""

from typing import Self


class AdapterError(Exception):
    """Base class of every error an adapter raises."""


class AdapterConfigError(AdapterError):
    """An adapter name, an option, or a reference is not usable."""


class InvalidInventoryError(AdapterError):
    """The inventory file is not valid. The message names the device and the key."""


class SourceOfTruthUnreachableError(AdapterError, OSError):
    """The source of truth does not answer, or refuses the request.

    The message names the setting to check, never a token or a request. The
    `errno` gives the log a fixed reason, such as `EACCES` for a refused token.
    """

    def __init__(self, message: str, code: int) -> None:
        """Hold the message and the errno."""
        super().__init__(message)
        self.errno = code

    def __reduce__(self) -> tuple[type[Self], tuple[str, int | None]]:
        """Keep the message and the errno in a pickle or a copy."""
        return type(self), (str(self), self.errno)


class DeviceNotFoundError(AdapterError):
    """The source of truth has no device with this id."""


class SecretNotFoundError(AdapterError):
    """A credential is missing. The message names where, never a value."""


class DeviceUnreachableError(AdapterError):
    """The session cannot reach the device or read what it asks for."""


class CommandNotAllowedError(AdapterError):
    """A command is not on the read-only allowlist of the platform."""


class InvalidTokenError(AdapterError):
    """The plan has no valid token from `validate_plan`."""


class DeviceWritesDisabledError(AdapterError):
    """Device writes are disabled. Phase 4 adds them."""
