"""Credentials, and the opaque references that stand in for them."""

from enum import StrEnum

from pydantic import SecretStr

from hco_core.domain.base import BaseModel


class SecretPurpose(StrEnum):
    """What a credential is for."""

    DEVICE_LOGIN = "device_login"
    DEVICE_ENABLE = "device_enable"
    API_KEY = "api_key"


class SecretRef(BaseModel):
    """An opaque handle to a credential.

    A `SourceOfTruth` returns one of these; only a `SecretsProvider` can resolve
    it. The indirection exists because inventory data and credential data need
    different handling — one is logged and cached, the other must never be.
    """

    provider: str
    key: str
    purpose: SecretPurpose = SecretPurpose.DEVICE_LOGIN


class DeviceCredentials(BaseModel):
    """A resolved device credential.

    `SecretStr` and the custom `__repr__` are not decoration. A device password
    in a traceback reaches the error tracker, the log aggregator, and possibly a
    support ticket.
    """

    username: str
    password: SecretStr | None = None
    private_key: SecretStr | None = None
    enable: SecretStr | None = None

    def __repr__(self) -> str:
        """Redacted, so an accidental log or traceback leaks nothing."""
        return f"DeviceCredentials(username={self.username!r}, ...)"

    def __str__(self) -> str:
        """Redacted. See `__repr__`."""
        return self.__repr__()
