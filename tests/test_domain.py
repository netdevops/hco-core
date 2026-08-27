"""Domain models are frozen, strict, and say what they mean."""

import pytest
from pydantic import ValidationError

from hco_core.domain import (
    ApplyStrategy,
    BaseModel,
    DeviceCredentials,
    DeviceID,
    RiskAssessment,
    RiskTier,
    TopologyEdge,
    TopologyGraph,
)


def test_models_are_frozen_and_strict() -> None:
    """Frozen so they are shareable; strict so a typo is an error."""
    creds = DeviceCredentials(username="admin")
    with pytest.raises(ValidationError):
        creds.username = "root"
    with pytest.raises(ValidationError):
        DeviceCredentials(username="admin", passwrod="typo")  # type: ignore[call-arg]


def test_credentials_never_render_their_secrets() -> None:
    """A password in a traceback reaches the error tracker and the log."""
    creds = DeviceCredentials(username="admin", password="hunter2")  # type: ignore[arg-type]  # ruff: ignore[hardcoded-password-func-arg]
    assert "hunter2" not in repr(creds)
    assert "hunter2" not in str(creds)


def test_risk_tiers_compare_so_a_floor_can_win() -> None:
    """The reconciliation rule is max(floor, assessed). It needs ordering."""
    assert RiskTier.CRITICAL > RiskTier.HIGH > RiskTier.MEDIUM > RiskTier.LOW
    assert max(RiskTier.LOW, RiskTier.HIGH) is RiskTier.HIGH


def test_risk_assessment_has_no_field_for_commands() -> None:
    """C4. The schema is the enforcement: an analyser does not write config."""
    forbidden = {"commands", "plan", "remediation", "config", "lines"}
    assert not (forbidden & set(RiskAssessment.model_fields))


def test_only_confirm_timer_strategies_revert_unattended() -> None:
    """Strict change sets need devices that undo themselves without us."""
    assert ApplyStrategy.IOSXR_COMMIT_CONFIRMED.reverts_without_orchestrator
    assert ApplyStrategy.EOS_SESSION_COMMIT_TIMER.reverts_without_orchestrator
    # NX-OS rollback and IOS configure-replace both need a reachable device.
    assert not ApplyStrategy.NXOS_CHECKPOINT.reverts_without_orchestrator
    assert not ApplyStrategy.IOS_ARCHIVE_REPLACE.reverts_without_orchestrator
    assert not ApplyStrategy.HCO_TIMED_REVERT.reverts_without_orchestrator


def test_topology_answers_the_questions_a_risk_floor_asks() -> None:
    """Single-homed and shared-redundancy both raise the floor."""
    a, b, c = DeviceID("a"), DeviceID("b"), DeviceID("c")
    g = TopologyGraph(
        nodes=(a, b, c),
        edges=(TopologyEdge(a=a, b=b),),
        redundancy_groups={"hsrp-1": (b, c)},
    )
    assert g.neighbors(a) == (b,)
    assert g.is_single_homed(a)
    assert not g.is_single_homed(b) or len(g.neighbors(b)) <= 1
    assert g.shares_group(b, c)
    assert not g.shares_group(a, b)


def test_base_model_is_the_local_one() -> None:
    """Every domain type must subclass the project base, not pydantic's."""
    assert issubclass(DeviceCredentials, BaseModel)
