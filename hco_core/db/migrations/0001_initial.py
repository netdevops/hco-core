"""Make the eight Phase 1 tables and the two self-approval triggers.

Revision ID: 0001
Revises: none
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.schema import SchemaItem

revision = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

SCHEMA = "hco"
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

REJECT_SELF_APPROVAL = """
CREATE OR REPLACE FUNCTION hco.reject_self_approval()
RETURNS TRIGGER AS $$
DECLARE v_requester TEXT;
BEGIN
  SELECT ar.requester INTO v_requester
    FROM hco.approval_request ar WHERE ar.id = NEW.request_id;
  IF v_requester = NEW.actor THEN
    RAISE EXCEPTION 'requester % may not approve their own change', NEW.actor;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql
"""

APPROVAL_DECISION_TRIGGER = """
CREATE TRIGGER approval_decision_reject_self_approval
BEFORE INSERT OR UPDATE OF request_id, actor ON hco.approval_decision
FOR EACH ROW EXECUTE FUNCTION hco.reject_self_approval()
"""

REJECT_REQUESTER_CHANGE = """
CREATE OR REPLACE FUNCTION hco.reject_requester_change()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.requester IS DISTINCT FROM OLD.requester THEN
    RAISE EXCEPTION 'the requester of approval request % may not change', OLD.id;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql
"""

APPROVAL_REQUEST_TRIGGER = """
CREATE TRIGGER approval_request_reject_requester_change
BEFORE UPDATE OF requester ON hco.approval_request
FOR EACH ROW EXECUTE FUNCTION hco.reject_requester_change()
"""


def trimmed(table: str, column: str) -> sa.CheckConstraint:
    """Return the named `CHECK` that refuses white space at an end and an empty name."""
    return sa.CheckConstraint(
        f"{column} !~ '^[[:space:]]' AND {column} !~ '[[:space:]]$' AND {column} <> ''",
        name=op.f(f"ck_{table}_{column}_trimmed"),
    )


def one_of(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    """Return the named `CHECK` that holds the column to the values."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(
        f"{column} IN ({quoted})", name=op.f(f"ck_{table}_{column}")
    )


def shared_columns(table: str) -> list[SchemaItem]:
    """Return the key, the tenant, and the two timestamps of every table."""
    return [
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("tenant_id", sa.String(26), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
    ]


def foreign_key(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    """Return the named foreign key from the column to `<target>.id`."""
    return sa.ForeignKeyConstraint(
        [column], [f"{SCHEMA}.{target}.id"], name=f"fk_{table}_{column}"
    )


def timestamp(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    """Return a `timestamptz` column, with `now()` as the default when required."""
    default = None if nullable else sa.func.now()
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=default, nullable=nullable
    )


def upgrade() -> None:
    """Make the tables, the indexes, and the trigger."""
    op.create_table(
        "device",
        *shared_columns("device"),
        sa.Column("sot_name", sa.String(64), nullable=False),
        sa.Column("sot_device_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("platform_name", sa.String(64), nullable=False),
        sa.Column("config_format", sa.String(8), server_default="cli", nullable=False),
        sa.Column("mgmt_address", sa.String(255), nullable=False),
        sa.Column("role", sa.String(128), nullable=True),
        sa.Column("site", sa.String(128), nullable=True),
        sa.Column(
            "criticality", sa.String(16), server_default="normal", nullable=False
        ),
        sa.Column("tags", JSONB, server_default="[]", nullable=False),
        sa.Column("in_maintenance", sa.Boolean, server_default="false", nullable=False),
        sa.Column("metadata", JSONB, server_default="{}", nullable=False),
        timestamp("last_synced_at"),
        sa.UniqueConstraint(
            "tenant_id",
            "sot_name",
            "sot_device_id",
            name="uq_device_tenant_id_sot_name_sot_device_id",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "platform_name = upper(platform_name) AND platform_name !~ '^[0-9]+$'",
            name=op.f("ck_device_platform_name"),
        ),
        one_of("device", "config_format", ("cli", "xml", "json")),
        one_of("device", "criticality", ("low", "normal", "high", "critical")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_device_tenant_id_platform_name",
        "device",
        ["tenant_id", "platform_name"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_device_tenant_id_site", "device", ["tenant_id", "site"], schema=SCHEMA
    )
    op.create_index(
        "ix_device_tags", "device", ["tags"], schema=SCHEMA, postgresql_using="gin"
    )

    op.create_table(
        "config_snapshot",
        *shared_columns("config_snapshot"),
        sa.Column("device_id", sa.String(26), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("body_location", sa.String(512), nullable=True),
        sa.Column("body_inline", sa.Text, nullable=True),
        sa.Column("dump", JSONB, nullable=False),
        sa.Column("device_fingerprint", sa.String(255), nullable=True),
        timestamp("collected_at"),
        foreign_key("config_snapshot", "device_id", "device"),
        one_of(
            "config_snapshot", "kind", ("running", "intended", "future", "post_change")
        ),
        one_of("config_snapshot", "format", ("cli", "xml", "json")),
        one_of("config_snapshot", "source", ("device", "git", "sot", "upload")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_config_snapshot_tenant_id_device_id_kind_collected_at",
        "config_snapshot",
        ["tenant_id", "device_id", "kind", sa.literal_column("collected_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_config_snapshot_body_sha256",
        "config_snapshot",
        ["body_sha256"],
        schema=SCHEMA,
    )

    op.create_table(
        "change_set",
        *shared_columns("change_set"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, server_default="", nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("transaction_class", sa.String(16), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("policy_verdict", JSONB, nullable=True),
        sa.Column("parent_id", sa.String(26), nullable=True),
        timestamp("scheduled_for", nullable=True),
        sa.Column("window_id", sa.String(26), nullable=True),
        foreign_key("change_set", "parent_id", "change_set"),
        trimmed("change_set", "requested_by"),
        one_of("change_set", "transaction_class", ("strict", "best_effort")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_change_set_tenant_id_state",
        "change_set",
        ["tenant_id", "state"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_change_set_tenant_id_requested_by",
        "change_set",
        ["tenant_id", "requested_by"],
        schema=SCHEMA,
    )

    op.create_table(
        "remediation_run",
        *shared_columns("remediation_run"),
        sa.Column("change_set_id", sa.String(26), nullable=False),
        sa.Column("device_id", sa.String(26), nullable=False),
        sa.Column("state", sa.String(32), server_default="DRAFT", nullable=False),
        sa.Column("running_snapshot_id", sa.String(26), nullable=True),
        sa.Column("intended_snapshot_id", sa.String(26), nullable=True),
        sa.Column("future_snapshot_id", sa.String(26), nullable=True),
        sa.Column("remediation_dump", JSONB, nullable=True),
        sa.Column("rollback_dump", JSONB, nullable=True),
        sa.Column("future_report", JSONB, nullable=True),
        sa.Column("change_fingerprint", sa.String(64), nullable=True),
        sa.Column(
            "plan_source", sa.String(16), server_default="deterministic", nullable=False
        ),
        sa.Column("risk_tier_floor", sa.String(16), nullable=True),
        sa.Column("risk_tier_final", sa.String(16), nullable=True),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("error", JSONB, nullable=True),
        foreign_key("remediation_run", "change_set_id", "change_set"),
        foreign_key("remediation_run", "device_id", "device"),
        foreign_key("remediation_run", "running_snapshot_id", "config_snapshot"),
        foreign_key("remediation_run", "intended_snapshot_id", "config_snapshot"),
        foreign_key("remediation_run", "future_snapshot_id", "config_snapshot"),
        one_of("remediation_run", "state", RUN_STATES),
        one_of("remediation_run", "plan_source", ("deterministic", "ai", "manual")),
        one_of("remediation_run", "risk_tier_floor", RISK_TIERS),
        one_of("remediation_run", "risk_tier_final", RISK_TIERS),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_remediation_run_tenant_id_state",
        "remediation_run",
        ["tenant_id", "state"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_remediation_run_change_set_id",
        "remediation_run",
        ["change_set_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_remediation_run_change_fingerprint",
        "remediation_run",
        ["change_fingerprint"],
        schema=SCHEMA,
    )

    op.create_table(
        "approval_request",
        *shared_columns("approval_request"),
        sa.Column("change_set_id", sa.String(26), nullable=False),
        sa.Column("state", sa.String(16), server_default="pending", nullable=False),
        sa.Column("required_count", sa.Integer, nullable=False),
        sa.Column("required_roles", JSONB, server_default="[]", nullable=False),
        sa.Column("requester", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interrupt_key", sa.String(64), nullable=False),
        foreign_key("approval_request", "change_set_id", "change_set"),
        sa.UniqueConstraint("change_set_id", name="uq_approval_request_change_set_id"),
        trimmed("approval_request", "requester"),
        one_of(
            "approval_request",
            "state",
            ("pending", "approved", "rejected", "expired", "cancelled"),
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "approval_decision",
        *shared_columns("approval_decision"),
        sa.Column("request_id", sa.String(26), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("surface", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text, server_default="", nullable=False),
        foreign_key("approval_decision", "request_id", "approval_request"),
        sa.UniqueConstraint(
            "request_id", "actor", name="uq_approval_decision_request_id_actor"
        ),
        trimmed("approval_decision", "actor"),
        one_of("approval_decision", "actor_kind", ACTOR_KINDS),
        one_of("approval_decision", "decision", ("approve", "reject")),
        one_of("approval_decision", "surface", SURFACES),
        schema=SCHEMA,
    )
    op.execute(REJECT_SELF_APPROVAL)
    op.execute(APPROVAL_DECISION_TRIGGER)
    op.execute(REJECT_REQUESTER_CHANGE)
    op.execute(APPROVAL_REQUEST_TRIGGER)

    op.create_table(
        "audit_event",
        *shared_columns("audit_event"),
        timestamp("occurred_at"),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("surface", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object_type", sa.String(64), nullable=False),
        sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("before", JSONB, nullable=True),
        sa.Column("after", JSONB, nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        one_of("audit_event", "actor_kind", ACTOR_KINDS),
        one_of("audit_event", "surface", SURFACES),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_event_tenant_id_occurred_at",
        "audit_event",
        ["tenant_id", sa.literal_column("occurred_at DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_event_object_type_object_id",
        "audit_event",
        ["object_type", "object_id"],
        schema=SCHEMA,
    )

    op.create_table(
        "notification_delivery",
        *shared_columns("notification_delivery"),
        sa.Column("run_id", sa.String(26), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("handle", JSONB, nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        timestamp("sent_at", nullable=True),
        foreign_key("notification_delivery", "run_id", "remediation_run"),
        sa.UniqueConstraint(
            "run_id",
            "channel",
            "purpose",
            name="uq_notification_delivery_run_id_channel_purpose",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Drop the triggers, their functions, and the tables."""
    op.execute(
        "DROP TRIGGER approval_request_reject_requester_change ON hco.approval_request"
    )
    op.execute("DROP FUNCTION hco.reject_requester_change()")
    op.execute(
        "DROP TRIGGER approval_decision_reject_self_approval ON hco.approval_decision"
    )
    op.execute("DROP FUNCTION hco.reject_self_approval()")
    for table in (
        "notification_delivery",
        "audit_event",
        "approval_decision",
        "approval_request",
        "remediation_run",
        "change_set",
        "config_snapshot",
        "device",
    ):
        op.drop_table(table, schema=SCHEMA)
