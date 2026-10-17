"""The eight tables of the hco-core schema, as docs/spec/03 defines them.

Every table has a ULID key, `created_at`, `updated_at`, and a nullable
`tenant_id`. An enumeration is a `String` with a `CHECK`, so a new value needs
no migration lock.
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from hco_core.db.types import ULID_LENGTH, PlatformName, new_id

SCHEMA = "hco"

type JSONObject = dict[str, object]

RUN_STATES = (
    "DRAFT",
    "COLLECTING",
    "REMEDIATING",
    "ASSESSING",
    "EVALUATING_POLICY",
    "BLOCKED",
    "AWAITING_APPROVAL",
    "REJECTED",
    "EXPIRED",
    "APPROVED",
    "SCHEDULED",
    "APPLYING",
    "VERIFYING",
    "COMMITTED",
    "ROLLING_BACK",
    "ROLLED_BACK",
    "ROLLBACK_FAILED",
)
RISK_TIERS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
ACTOR_KINDS = ("user", "agent", "system")
SURFACES = ("ui", "pr", "chat", "api", "cli", "mcp")


def one_of(name: str, column: str, values: Sequence[str]) -> CheckConstraint:
    """Return a `CHECK` that holds the column to the given values."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


class Base(DeclarativeBase):
    """Give every table the schema `hco`, the shared columns, and stable names."""

    metadata = MetaData(
        schema=SCHEMA,
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        },
    )

    id: Mapped[str] = mapped_column(
        String(ULID_LENGTH), primary_key=True, default=new_id, sort_order=-2
    )
    tenant_id: Mapped[str | None] = mapped_column(String(ULID_LENGTH), sort_order=-1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Device(Base):
    """A device that a source of truth reported."""

    __tablename__ = "device"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "sot_name", "sot_device_id", postgresql_nulls_not_distinct=True
        ),
        Index(None, "tenant_id", "platform_name"),
        Index(None, "tenant_id", "site"),
        Index(None, "tags", postgresql_using="gin"),
        one_of("config_format", "config_format", ("cli", "xml", "json")),
        one_of("criticality", "criticality", ("low", "normal", "high", "critical")),
    )

    sot_name: Mapped[str] = mapped_column(String(64))
    sot_device_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    platform_name: Mapped[str] = mapped_column(PlatformName)
    config_format: Mapped[str] = mapped_column(String(8), server_default="cli")
    mgmt_address: Mapped[str] = mapped_column(String(255))
    role: Mapped[str | None] = mapped_column(String(128))
    site: Mapped[str | None] = mapped_column(String(128))
    criticality: Mapped[str] = mapped_column(String(16), server_default="normal")
    tags: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    in_maintenance: Mapped[bool] = mapped_column(Boolean, server_default="false")
    metadata_: Mapped[JSONObject] = mapped_column(
        "metadata", JSONB, server_default="{}"
    )
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConfigSnapshot(Base):
    """A configuration as the faithful text and as the parsed `Dump` JSON."""

    __tablename__ = "config_snapshot"
    __table_args__ = (
        Index(
            "ix_config_snapshot_tenant_id_device_id_kind_collected_at",
            "tenant_id",
            "device_id",
            "kind",
            literal_column("collected_at DESC", DateTime),
        ),
        Index(None, "body_sha256"),
        one_of("kind", "kind", ("running", "intended", "future", "post_change")),
        one_of("format", "format", ("cli", "xml", "json")),
        one_of("source", "source", ("device", "git", "sot", "upload")),
    )

    device_id: Mapped[str] = mapped_column(ForeignKey("device.id"))
    kind: Mapped[str] = mapped_column(String(16))
    format: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(16))
    body_sha256: Mapped[str] = mapped_column(String(64))
    body_location: Mapped[str | None] = mapped_column(String(512))
    body_inline: Mapped[str | None] = mapped_column(Text)
    dump: Mapped[JSONObject] = mapped_column(JSONB)
    device_fingerprint: Mapped[str | None] = mapped_column(String(255))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChangeSet(Base):
    """A change that one person requests, over one or more runs."""

    __tablename__ = "change_set"
    __table_args__ = (
        Index(None, "tenant_id", "state"),
        Index(None, "tenant_id", "requested_by"),
        one_of("transaction_class", "transaction_class", ("strict", "best_effort")),
    )

    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, server_default="")
    requested_by: Mapped[str] = mapped_column(String(255))
    transaction_class: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(32))
    policy_verdict: Mapped[JSONObject | None] = mapped_column(JSONB)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("change_set.id"))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_id: Mapped[str | None] = mapped_column(String(ULID_LENGTH))


class RemediationRun(Base):
    """One device in a change set, from collection to its final state."""

    __tablename__ = "remediation_run"
    __table_args__ = (
        Index(None, "tenant_id", "state"),
        Index(None, "change_set_id"),
        Index(None, "change_fingerprint"),
        one_of("state", "state", RUN_STATES),
        one_of("plan_source", "plan_source", ("deterministic", "ai", "manual")),
        one_of("risk_tier_floor", "risk_tier_floor", RISK_TIERS),
        one_of("risk_tier_final", "risk_tier_final", RISK_TIERS),
    )

    change_set_id: Mapped[str] = mapped_column(ForeignKey("change_set.id"))
    device_id: Mapped[str] = mapped_column(ForeignKey("device.id"))
    state: Mapped[str] = mapped_column(String(32), server_default="DRAFT")
    running_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("config_snapshot.id")
    )
    intended_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("config_snapshot.id")
    )
    future_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("config_snapshot.id")
    )
    remediation_dump: Mapped[JSONObject | None] = mapped_column(JSONB)
    rollback_dump: Mapped[JSONObject | None] = mapped_column(JSONB)
    future_report: Mapped[JSONObject | None] = mapped_column(JSONB)
    change_fingerprint: Mapped[str | None] = mapped_column(String(64))
    plan_source: Mapped[str] = mapped_column(String(16), server_default="deterministic")
    risk_tier_floor: Mapped[str | None] = mapped_column(String(16))
    risk_tier_final: Mapped[str | None] = mapped_column(String(16))
    thread_id: Mapped[str] = mapped_column(String(64))
    error: Mapped[JSONObject | None] = mapped_column(JSONB)


class ApprovalRequest(Base):
    """The one approval that a change set waits for."""

    __tablename__ = "approval_request"
    __table_args__ = (
        UniqueConstraint("change_set_id"),
        one_of(
            "state",
            "state",
            ("pending", "approved", "rejected", "expired", "cancelled"),
        ),
    )

    change_set_id: Mapped[str] = mapped_column(ForeignKey("change_set.id"))
    state: Mapped[str] = mapped_column(String(16), server_default="pending")
    required_count: Mapped[int] = mapped_column(Integer)
    required_roles: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    requester: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interrupt_key: Mapped[str] = mapped_column(String(64))


class ApprovalDecision(Base):
    """One person's decision on a request. A trigger refuses the requester."""

    __tablename__ = "approval_decision"
    __table_args__ = (
        UniqueConstraint("request_id", "actor"),
        one_of("actor_kind", "actor_kind", ACTOR_KINDS),
        one_of("decision", "decision", ("approve", "reject")),
        one_of("surface", "surface", SURFACES),
    )

    request_id: Mapped[str] = mapped_column(ForeignKey("approval_request.id"))
    actor: Mapped[str] = mapped_column(String(255))
    actor_kind: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(16))
    surface: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str] = mapped_column(Text, server_default="")


class AuditEvent(Base):
    """One append-only record of who did what, on which surface."""

    __tablename__ = "audit_event"
    __table_args__ = (
        Index(
            "ix_audit_event_tenant_id_occurred_at",
            "tenant_id",
            literal_column("occurred_at DESC", DateTime),
        ),
        Index(None, "object_type", "object_id"),
        one_of("actor_kind", "actor_kind", ACTOR_KINDS),
        one_of("surface", "surface", SURFACES),
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(String(255))
    actor_kind: Mapped[str] = mapped_column(String(16))
    surface: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str] = mapped_column(String(64))
    before: Mapped[JSONObject | None] = mapped_column(JSONB)
    after: Mapped[JSONObject | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(String(64))


class NotificationDelivery(Base):
    """The delivery ledger: one row for each run, channel, and purpose."""

    __tablename__ = "notification_delivery"
    __table_args__ = (UniqueConstraint("run_id", "channel", "purpose"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("remediation_run.id"))
    channel: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32))
    handle: Mapped[JSONObject | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
