"""Credential resolution."""

from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from hco_core.domain.secrets import DeviceCredentials, SecretRef


@runtime_checkable
class SecretsProvider(Protocol):
    """Resolves an opaque `SecretRef` to a credential.

    Separate from `SourceOfTruth` on purpose: inventory data and credential data
    need different handling, and mixing them is how a password ends up in a log.
    """

    async def resolve(self, ref: SecretRef) -> DeviceCredentials:
        """Resolve a device credential."""
        ...

    async def resolve_api_key(self, ref: SecretRef) -> SecretStr:
        """Resolve an API key."""
        ...
