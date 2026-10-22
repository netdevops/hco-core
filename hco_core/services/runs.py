"""Write change sets, remediation runs, and config snapshots.

Every state transition writes one `audit_event` in the same transaction.
"""

import hashlib
from collections.abc import Collection
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hco_core.db import models
from hco_core.db.types import new_id
from hco_core.domain.approval import RunState
from hco_core.domain.config import ConfigFormat
from hco_core.domain.remediation import DumpJson
from hco_core.domain.risk import RiskTier
from hco_core.services.audit import Actor, AuditEntry, add_audit_event

type SnapshotKind = Literal["running", "intended", "future", "post_change"]
type SnapshotSource = Literal["device", "git", "sot", "upload"]

_DUMP: TypeAdapter[DumpJson] = TypeAdapter(DumpJson)


class RunNotFoundError(LookupError):
    """No remediation run or change set has the id."""


class RunStateError(RuntimeError):
    """The run is not in a state that permits the write."""


@dataclass(frozen=True, slots=True)
class RunView:
    """The state and the thread of a run, and the source of truth identity of its device."""

    state: RunState
    device_id: str
    sot_device_id: str
    platform_name: str
    thread_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunUpdate:
    """The run fields to set. A field left as `None` keeps its value."""

    state: RunState | None = None
    running_snapshot_id: str | None = None
    intended_snapshot_id: str | None = None
    future_snapshot_id: str | None = None
    remediation_dump: DumpJson | None = None
    rollback_dump: DumpJson | None = None
    future_report: models.JSONObject | None = None
    change_fingerprint: str | None = None
    risk_tier_floor: RiskTier | None = None
    risk_tier_final: RiskTier | None = None
    error: models.JSONObject | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Snapshot:
    """A configuration as its faithful text and its parsed `Dump` JSON."""

    device_id: str
    kind: SnapshotKind
    body: str
    dump: DumpJson
    format: ConfigFormat = ConfigFormat.CLI
    source: SnapshotSource = "device"
    device_fingerprint: str | None = None


class RunRepository:
    """Create and update the rows of a run, and audit each transition."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_change_set(
        self,
        title: str,
        requester: Actor,
        *,
        description: str = "",
        transaction_class: Literal["strict", "best_effort"] = "strict",
    ) -> str:
        """Create a change set in `DRAFT` and return its id.

        The requester is the actor. `ApprovalService.upsert_request` copies it.
        """
        async with self._sessions.begin() as session:
            change_set = models.ChangeSet(
                id=new_id(),
                title=title,
                description=description,
                requested_by=requester.name,
                transaction_class=transaction_class,
                state=RunState.DRAFT,
            )
            session.add(change_set)
            _audit_transition(session, requester, change_set, None)
            return change_set.id

    async def create_run(
        self, change_set_id: str, device_id: str, thread_id: str, actor: Actor
    ) -> str:
        """Create a run in `DRAFT` for one device and return its id."""
        async with self._sessions.begin() as session:
            run = models.RemediationRun(
                id=new_id(),
                change_set_id=change_set_id,
                device_id=device_id,
                thread_id=thread_id,
                state=RunState.DRAFT,
            )
            session.add(run)
            _audit_transition(session, actor, run, None)
            return run.id

    async def create_change_set_and_run(
        self, title: str, device_id: str, thread_id: str, requester: Actor
    ) -> tuple[str, str]:
        """Create a change set and its run in `DRAFT`, in one transaction.

        The return is the change set id and the run id.
        """
        async with self._sessions.begin() as session:
            change_set = models.ChangeSet(
                id=new_id(),
                title=title,
                requested_by=requester.name,
                transaction_class="strict",
                state=RunState.DRAFT,
            )
            session.add(change_set)
            _audit_transition(session, requester, change_set, None)
            await session.flush()
            run = models.RemediationRun(
                id=new_id(),
                change_set_id=change_set.id,
                device_id=device_id,
                thread_id=thread_id,
                state=RunState.DRAFT,
            )
            session.add(run)
            _audit_transition(session, requester, run, None)
            return change_set.id, run.id

    async def get_run(self, run_id: str) -> RunView:
        """Return the state of the run and the identity of its device.

        Raises:
            RunNotFoundError: If no run has the id.

        """
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(
                        models.RemediationRun.state,
                        models.Device.id,
                        models.Device.sot_device_id,
                        models.Device.platform_name,
                        models.RemediationRun.thread_id,
                    )
                    .join(models.Device)
                    .where(models.RemediationRun.id == run_id)
                )
            ).one_or_none()
        if row is None:
            message = f"no remediation run {run_id!r}"
            raise RunNotFoundError(message)
        return RunView(RunState(row[0]), row[1], row[2], row[3], row[4])

    async def update_run(
        self,
        run_id: str,
        update: RunUpdate,
        actor: Actor,
        *,
        only_from: Collection[RunState] | None = None,
    ) -> bool:
        """Set the given fields, and audit the state if it changes.

        With `only_from`, a run in another state stays unchanged. A missing run
        raises `RunNotFoundError`.
        """
        async with self._sessions.begin() as session:
            run = await _locked_run(session, run_id)
            if only_from is not None and run.state not in only_from:
                return False
            before = run.state
            _apply(run, update)
            _audit_transition(session, actor, run, before)
            return True

    async def record_collection(
        self, run_id: str, running: Snapshot, intended: Snapshot, actor: Actor
    ) -> tuple[DumpJson, DumpJson]:
        """Store the two snapshots and move the run to `COLLECTING`, one time.

        A run in `COLLECTING` keeps its snapshots, and the call returns their
        dumps. The return is the running and the intended dump. A missing run
        raises `RunNotFoundError`.

        Raises:
            RunStateError: If the run is in another state.

        """
        async with self._sessions.begin() as session:
            run = await _locked_run(session, run_id)
            if run.state == RunState.DRAFT:
                running_row = _snapshot_row(running)
                intended_row = _snapshot_row(intended)
                session.add_all([running_row, intended_row])
                await session.flush()
                run.running_snapshot_id = running_row.id
                run.intended_snapshot_id = intended_row.id
                run.state = RunState.COLLECTING
                _audit_transition(session, actor, run, RunState.DRAFT)
                return running.dump, intended.dump
            if run.state == RunState.COLLECTING:
                stored = [
                    await session.get(models.ConfigSnapshot, snapshot_id)
                    for snapshot_id in (
                        run.running_snapshot_id,
                        run.intended_snapshot_id,
                    )
                ]
                dumps = [_DUMP.validate_python(row.dump) for row in stored if row]
                if len(dumps) == len(stored):
                    return dumps[0], dumps[1]
            message = f"run {run_id!r} is {run.state}; collection needs DRAFT"
            raise RunStateError(message)

    async def record_remediation(
        self, run_id: str, future: Snapshot, update: RunUpdate, actor: Actor
    ) -> None:
        """Store the future snapshot and move the run to `REMEDIATING`, one time.

        A run in `REMEDIATING` stays unchanged. A missing run raises
        `RunNotFoundError`.

        Raises:
            RunStateError: If the run is not in `COLLECTING` or `REMEDIATING`.

        """
        async with self._sessions.begin() as session:
            run = await _locked_run(session, run_id)
            if run.state == RunState.REMEDIATING:
                return
            if run.state != RunState.COLLECTING:
                message = f"run {run_id!r} is {run.state}; remediation needs COLLECTING"
                raise RunStateError(message)
            future_row = _snapshot_row(future)
            session.add(future_row)
            await session.flush()
            _apply(
                run,
                replace(
                    update,
                    state=RunState.REMEDIATING,
                    future_snapshot_id=future_row.id,
                ),
            )
            _audit_transition(session, actor, run, RunState.COLLECTING)

    async def set_change_set_state(
        self, change_set_id: str, state: RunState, actor: Actor
    ) -> None:
        """Set the state of a change set, and audit the change.

        Raises:
            RunNotFoundError: If no change set has the id.

        """
        async with self._sessions.begin() as session:
            change_set = await session.get(
                models.ChangeSet, change_set_id, with_for_update=True
            )
            if change_set is None:
                message = f"no change set {change_set_id!r}"
                raise RunNotFoundError(message)
            before = change_set.state
            change_set.state = state
            _audit_transition(session, actor, change_set, before)

    async def add_snapshot(self, snapshot: Snapshot) -> str:
        """Store the snapshot with the sha256 of its body and return its id."""
        async with self._sessions.begin() as session:
            row = _snapshot_row(snapshot)
            session.add(row)
            return row.id


async def _locked_run(session: AsyncSession, run_id: str) -> models.RemediationRun:
    run = await session.get(models.RemediationRun, run_id, with_for_update=True)
    if run is None:
        message = f"no remediation run {run_id!r}"
        raise RunNotFoundError(message)
    return run


def _snapshot_row(snapshot: Snapshot) -> models.ConfigSnapshot:
    return models.ConfigSnapshot(
        id=new_id(),
        device_id=snapshot.device_id,
        kind=snapshot.kind,
        format=snapshot.format,
        source=snapshot.source,
        body_sha256=hashlib.sha256(snapshot.body.encode()).hexdigest(),
        body_inline=snapshot.body,
        dump=dict(snapshot.dump),
        device_fingerprint=snapshot.device_fingerprint,
    )


def _apply(run: models.RemediationRun, update: RunUpdate) -> None:
    values: dict[str, object] = {
        "state": update.state,
        "running_snapshot_id": update.running_snapshot_id,
        "intended_snapshot_id": update.intended_snapshot_id,
        "future_snapshot_id": update.future_snapshot_id,
        "remediation_dump": _json(update.remediation_dump),
        "rollback_dump": _json(update.rollback_dump),
        "future_report": update.future_report,
        "change_fingerprint": update.change_fingerprint,
        "risk_tier_floor": _tier_name(update.risk_tier_floor),
        "risk_tier_final": _tier_name(update.risk_tier_final),
        "error": update.error,
    }
    for name, value in values.items():
        if value is not None:
            setattr(run, name, value)


def _json(dump: DumpJson | None) -> models.JSONObject | None:
    return None if dump is None else dict(dump)


def _tier_name(tier: RiskTier | None) -> str | None:
    return None if tier is None else tier.name


def _audit_transition(
    session: AsyncSession,
    actor: Actor,
    row: models.ChangeSet | models.RemediationRun,
    before: str | None,
) -> None:
    if before == row.state:
        return
    verb = "create" if before is None else "transition"
    add_audit_event(
        session,
        AuditEntry(
            actor=actor,
            action=f"{row.__tablename__}.{verb}",
            object_type=row.__tablename__,
            object_id=row.id,
            before=None if before is None else {"state": before},
            after={"state": row.state},
        ),
    )
