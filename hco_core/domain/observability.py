"""Health, metrics, and alerts."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from hco_core.domain.base import BaseModel


class HealthStatus(BaseModel):
    """Whether a dependency is usable right now."""

    healthy: bool
    detail: str = ""
    checked_at: datetime | None = None


class Threshold(BaseModel):
    """When a metric change counts as a regression.

    Deterministic and numeric on purpose. This feeds a post-change gate, so it
    must never be a judgement call.
    """

    metric: str
    max_increase_pct: float | None = None
    max_decrease_pct: float | None = None
    absolute_max: float | None = None
    absolute_min: float | None = None


class MetricWindow(BaseModel):
    """Metric samples over a period, used as a before-and-after baseline."""

    device_id: str
    start: datetime
    end: datetime
    series: Mapping[str, tuple[float, ...]] = {}


class Alert(BaseModel):
    """An active alert.

    UNTRUSTED. Labels and annotations are operator-authored free text and reach
    a prompt, so they are fenced like any other untrusted input.
    """

    name: str
    severity: str = ""
    labels: Mapping[str, str] = {}
    annotations: Mapping[str, str] = {}
    started_at: datetime | None = None


class HealthDelta(BaseModel):
    """Whether health regressed across a change."""

    regressed: bool
    breached: tuple[str, ...] = ()
    detail: Mapping[str, Any] = {}
