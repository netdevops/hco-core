"""The nine ports.

Everything that touches the outside world implements one of these. They are
`typing.Protocol`, so an implementation needs no base class and a third party
can satisfy one without importing anything from here at runtime.
"""

from hco_core.ports.config_backend import ConfigBackend
from hco_core.ports.connection import ConnectionDriver, DeviceSession
from hco_core.ports.llm_provider import LLMProvider
from hco_core.ports.notification import NotificationChannel
from hco_core.ports.planner import RemediationPlanner
from hco_core.ports.secrets import SecretsProvider
from hco_core.ports.source_of_truth import SourceOfTruth
from hco_core.ports.telemetry import TelemetryProvider
from hco_core.ports.vector_store import VectorStore

__all__ = (
    "ConfigBackend",
    "ConnectionDriver",
    "DeviceSession",
    "LLMProvider",
    "NotificationChannel",
    "RemediationPlanner",
    "SecretsProvider",
    "SourceOfTruth",
    "TelemetryProvider",
    "VectorStore",
)
