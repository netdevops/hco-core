"""The approvals that each risk tier needs, until policy becomes data.

Phase 1 checks no roles, because it has no identity provider.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from hco_core.domain.risk import RiskTier

APPROVALS_BY_TIER: Final[Mapping[RiskTier, int]] = MappingProxyType(
    {RiskTier.LOW: 1, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2, RiskTier.CRITICAL: 2}
)
"""The smallest number of approvals that closes a request at each tier."""

REQUESTER_MAY_APPROVE: Final = False
"""The requester never counts toward the quorum, at any tier. No setting changes it."""
