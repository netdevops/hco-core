"""Where configurations are read from, and proposals written to."""

from typing import Protocol, runtime_checkable

from hco_core.domain.config import ConfigDocument
from hco_core.domain.device import DeviceRecord
from hco_core.domain.notify import ChangeProposal, ProposalHandle, ProposalStatus


@runtime_checkable
class ConfigBackend(Protocol):
    """A repository of device configurations."""

    async def fetch(self, device: DeviceRecord) -> ConfigDocument | None:
        """Read a stored configuration.

        It may be stale. A caller must check freshness before building a change
        from it.
        """
        ...

    async def store(self, device: DeviceRecord, config: ConfigDocument) -> str:
        """Store a configuration and return a reference."""
        ...

    async def propose_change(self, request: ChangeProposal) -> ProposalHandle:
        """Open a proposal whose diff is the remediation."""
        ...

    async def update_proposal_status(
        self, handle: ProposalHandle, status: ProposalStatus, summary: str
    ) -> None:
        """Reflect a decision back onto the proposal."""
        ...
