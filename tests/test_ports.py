"""The ports are protocols, and a plain object can satisfy one."""

from typing import get_type_hints

import pytest

from hco_core.ports import (
    ConfigBackend,
    ConnectionDriver,
    DeviceSession,
    LLMProvider,
    NotificationChannel,
    RemediationPlanner,
    SecretsProvider,
    SourceOfTruth,
    TelemetryProvider,
    VectorStore,
)

ALL_PORTS = (
    ConfigBackend,
    ConnectionDriver,
    DeviceSession,
    LLMProvider,
    NotificationChannel,
    RemediationPlanner,
    SecretsProvider,
    SourceOfTruth,
    TelemetryProvider,
    VectorStore,
)


@pytest.mark.parametrize("port", ALL_PORTS)
def test_port_is_runtime_checkable(port: type) -> None:
    """isinstance() must work, so an adapter registry can validate at startup."""
    assert getattr(port, "_is_runtime_protocol", False), (
        f"{port.__name__} must be @runtime_checkable"
    )


@pytest.mark.parametrize("port", ALL_PORTS)
def test_port_methods_are_annotated(port: type) -> None:
    """Every method carries full type hints. A contract without types is prose."""
    for name, member in vars(port).items():
        if name.startswith("_") or not callable(member):
            continue
        hints = get_type_hints(member)
        assert "return" in hints, f"{port.__name__}.{name} has no return type"


def test_a_plain_object_satisfies_the_planner_contract() -> None:
    """No base class, no import at runtime -- structural typing only.

    This is the property that lets a third party implement the contract without
    taking a dependency on this package at runtime.
    """

    class Analyser:
        async def propose(self, request: object) -> object:
            """Stub."""

        async def assess(
            self,
            change: object,
            evidence: object,
            *,
            binding: object,
            prompt_version: object,
        ) -> object:
            """Stub."""

        async def explain(
            self, change: object, *, binding: object, prompt_version: object
        ) -> object:
            """Stub."""

    assert isinstance(Analyser(), RemediationPlanner)


def test_an_incomplete_implementation_does_not_satisfy_it() -> None:
    """Missing a method must fail the check, or the check is worthless."""

    class Partial:
        async def propose(self, request: object) -> object:
            """Stub."""

    assert not isinstance(Partial(), RemediationPlanner)
