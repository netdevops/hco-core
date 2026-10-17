"""Decide whether a stored running config is fresh enough for a risk tier."""

from hco_core.domain.risk import RiskTier
from hco_core.domain.validation import FreshnessVerdict


def freshness_verdict(
    tier: RiskTier,
    snapshot_fingerprint: str | None,
    device_fingerprint: str | None,
) -> FreshnessVerdict:
    """Return the verdict of the tier table in the ports and adapters spec.

    LOW and MEDIUM use the snapshot when both fingerprints are present and
    equal. HIGH and CRITICAL always collect again. A missing or empty
    fingerprint counts as a mismatch.
    """
    if (
        tier < RiskTier.HIGH
        and snapshot_fingerprint
        and snapshot_fingerprint == device_fingerprint
    ):
        return FreshnessVerdict.USE_SNAPSHOT
    return FreshnessVerdict.RECOLLECT
