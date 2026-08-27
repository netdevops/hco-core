"""Reading operational state from a monitoring system."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from hco_core.domain.device import DeviceRecord
from hco_core.domain.observability import Alert, HealthDelta, MetricWindow, Threshold


@runtime_checkable
class TelemetryProvider(Protocol):
    """Metrics and alerts for a device."""

    async def window(
        self,
        device: DeviceRecord,
        metrics: Sequence[str],
        *,
        start: datetime,
        end: datetime,
    ) -> MetricWindow:
        """Fetch metric samples over a period."""
        ...

    async def active_alerts(self, device: DeviceRecord) -> tuple[Alert, ...]:
        """Alerts firing now. Labels and annotations are untrusted."""
        ...

    async def compare(
        self,
        *,
        before: MetricWindow,
        after: MetricWindow,
        thresholds: Mapping[str, Threshold],
    ) -> HealthDelta:
        """Decide whether health regressed.

        Deterministic and threshold-driven. This is a gate, so it must never be
        a judgement call.
        """
        ...
