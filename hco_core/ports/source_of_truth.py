"""Where intent and inventory live."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from hco_core.domain.config import ConfigDocument
from hco_core.domain.device import (
    DevicePage,
    DeviceRecord,
    DeviceSelector,
    TopologyGraph,
)
from hco_core.domain.ids import DeviceID
from hco_core.domain.observability import HealthStatus
from hco_core.domain.secrets import SecretPurpose, SecretRef


@runtime_checkable
class SourceOfTruth(Protocol):
    """Inventory, intent, topology, and metadata.

    Returns an OPAQUE reference to credentials, never a credential. Inventory
    responses are logged, cached, and rendered in a user interface, and a secret
    must not be in any of those.
    """

    @property
    def name(self) -> str:
        """Which adapter this is."""
        ...

    async def health(self) -> HealthStatus:
        """Check reachability and authentication."""
        ...

    async def list_devices(
        self,
        *,
        selector: DeviceSelector | None = None,
        cursor: str | None = None,
        limit: int = 500,
    ) -> DevicePage:
        """Page through devices."""
        ...

    async def get_device(self, device_id: DeviceID) -> DeviceRecord:
        """Fetch one device."""
        ...

    async def intended_config(self, device_id: DeviceID) -> ConfigDocument | None:
        """Return the configuration this device should hold, or None."""
        ...

    async def config_context(self, device_id: DeviceID) -> Mapping[str, Any]:
        """Structured data about the device that is not configuration."""
        ...

    async def topology(self, device_ids: Sequence[DeviceID]) -> TopologyGraph:
        """Neighbours, circuits, and redundancy relationships.

        This is what turns "removes an ACL" into "removes the ACL on the only
        WAN edge at a single-homed site".
        """
        ...

    async def secret_ref(
        self, device_id: DeviceID, purpose: SecretPurpose
    ) -> SecretRef:
        """Return an opaque handle, resolvable only by a `SecretsProvider`."""
        ...
