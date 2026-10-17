"""The freshness verdict follows the stated tier table of the git-sourced config."""

import pytest

from hco_core.domain.risk import RiskTier
from hco_core.domain.validation import FreshnessVerdict
from hco_core.engine.freshness import freshness_verdict

USE = FreshnessVerdict.USE_SNAPSHOT
RECOLLECT = FreshnessVerdict.RECOLLECT


@pytest.mark.parametrize(
    ("tier", "snapshot", "device", "verdict"),
    [
        (RiskTier.LOW, "fp-1", "fp-1", USE),
        (RiskTier.MEDIUM, "fp-1", "fp-1", USE),
        (RiskTier.LOW, "fp-1", "fp-2", RECOLLECT),
        (RiskTier.MEDIUM, "fp-1", "fp-2", RECOLLECT),
        (RiskTier.HIGH, "fp-1", "fp-1", RECOLLECT),
        (RiskTier.CRITICAL, "fp-1", "fp-1", RECOLLECT),
        (RiskTier.LOW, None, "fp-1", RECOLLECT),
        (RiskTier.LOW, "fp-1", None, RECOLLECT),
        (RiskTier.MEDIUM, None, None, RECOLLECT),
        (RiskTier.LOW, "", "", RECOLLECT),
    ],
)
def test_freshness_verdict_follows_the_tier_table(
    tier: RiskTier,
    snapshot: str | None,
    device: str | None,
    verdict: FreshnessVerdict,
) -> None:
    """LOW and MEDIUM use a matching snapshot; anything else is collected again."""
    assert freshness_verdict(tier, snapshot, device) is verdict
