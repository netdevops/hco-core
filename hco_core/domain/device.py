"""Devices, inventory pages, and topology."""

from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from hco_core.domain.base import BaseModel
from hco_core.domain.config import ConfigFormat
from hco_core.domain.ids import DeviceID


class DeviceRecord(BaseModel):
    """One device, as the source of truth describes it.

    Carries no credentials. A `SourceOfTruth` returns an opaque `SecretRef`
    instead, because inventory responses are logged, cached, and rendered in a
    user interface, and a credential must not be in any of those.
    """

    id: DeviceID
    name: str
    platform_name: str
    """A `hier_config` platform NAME — `Platform.CISCO_IOS.name` — or a custom
    platform string registered with `register_driver()`.

    Never `Platform.value`. `Platform` is `(str, Enum)` with `auto()`, so its
    values are the positional counters "1" through "13". Adding a member
    upstream renumbers every value after it, and previously stored rows silently
    change meaning.
    """
    mgmt_address: str
    role: str | None = None
    site: str | None = None
    criticality: Literal["low", "normal", "high", "critical"] = "normal"
    tags: frozenset[str] = frozenset()
    config_format: ConfigFormat = ConfigFormat.CLI
    in_maintenance: bool = False
    metadata: Mapping[str, str] = Field(default_factory=dict)


class DeviceSelector(BaseModel):
    """Which devices a query or a group covers.

    Fields AND together. An empty selector matches everything, which is why
    callers that build one from user input should refuse the empty case rather
    than silently targeting the fleet.
    """

    ids: tuple[DeviceID, ...] = ()
    names: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()
    tags_any: frozenset[str] = frozenset()
    tags_all: frozenset[str] = frozenset()


class DevicePage(BaseModel):
    """One page of devices, with a cursor for the next."""

    devices: tuple[DeviceRecord, ...]
    cursor: str | None = None
    total: int | None = None


class TopologyEdge(BaseModel):
    """A link between two devices."""

    a: DeviceID
    b: DeviceID
    kind: Literal["cable", "circuit", "logical", "wireless"] = "cable"
    a_interface: str | None = None
    b_interface: str | None = None
    circuit_id: str | None = None
    provider: str | None = None


class TopologyGraph(BaseModel):
    """Neighbours and redundancy, from the source of truth.

    This is what turns "removes an ACL" into "removes the ACL on the only WAN
    edge at a single-homed site". Without it, impact analysis is guessing.
    """

    nodes: tuple[DeviceID, ...]
    edges: tuple[TopologyEdge, ...] = ()
    redundancy_groups: Mapping[str, tuple[DeviceID, ...]] = Field(default_factory=dict)

    def neighbors(self, device: DeviceID) -> tuple[DeviceID, ...]:
        """Every device directly linked to this one."""
        found = [e.b for e in self.edges if e.a == device]
        found += [e.a for e in self.edges if e.b == device]
        return tuple(dict.fromkeys(found))

    def is_single_homed(self, device: DeviceID) -> bool:
        """Report whether the device has at most one neighbour.

        A single-homed device has no path that survives the change going wrong,
        which is why it raises the deterministic risk floor.
        """
        return len(self.neighbors(device)) <= 1

    def shares_group(self, a: DeviceID, b: DeviceID) -> bool:
        """Report whether both devices belong to one redundancy group.

        Applying to both halves of a redundant pair in the same wave defeats the
        redundancy, so this forces a wave split.
        """
        return any(
            a in members and b in members for members in self.redundancy_groups.values()
        )
