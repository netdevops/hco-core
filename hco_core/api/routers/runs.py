"""The run routes: create a run that parks for approval, and read runs and diffs.

No route here writes to a device. A run stops at `AWAITING_APPROVAL`.
"""

import asyncio
import re
from datetime import datetime
from itertools import starmap
from typing import Annotated, Final, cast

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status
from pydantic import Field
from sqlalchemy import Select, select

from hco_core.api.actor import current_actor
from hco_core.api.deps import API_PREFIX, DEFAULT_PAGE, ApiDeps, Cursor, Limit
from hco_core.api.errors import RunNotReadyError
from hco_core.api.routers.devices import find_device
from hco_core.db import models
from hco_core.db.types import new_id
from hco_core.domain.approval import RunState
from hco_core.domain.base import BaseModel
from hco_core.engine.remediation import render_lines
from hco_core.graphs.checkpointer import open_checkpointer
from hco_core.graphs.review import ReviewState, build_review_graph, start_run
from hco_core.services.audit import Actor
from hco_core.services.runs import RunNotFoundError
from hco_core.settings import CoreSettings

MAX_ERRORS: Final = 20
MAX_ERROR_CHARACTERS: Final = 500
_URL_CREDENTIALS: Final = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")

_log = structlog.get_logger(__name__)


class CreateRun(BaseModel):
    """The device of the run, by row id or source of truth id, and a title."""

    device_id: Annotated[str, Field(min_length=1, max_length=255)]
    title: Annotated[str | None, Field(min_length=1, max_length=255)] = None


class RunCreated(BaseModel):
    """The ids of the new run. The graph continues after the response."""

    run_id: str
    change_set_id: str
    state: RunState


class RunSummary(BaseModel):
    """The state, the device, the requester, and the tiers of one run."""

    id: str
    change_set_id: str
    device_id: str
    sot_device_id: str
    device_name: str
    state: RunState
    requested_by: str
    risk_tier_floor: str | None
    risk_tier_final: str | None
    created_at: datetime
    updated_at: datetime


class RunDetail(RunSummary):
    """One run with its fingerprint and its errors."""

    plan_source: str
    change_fingerprint: str | None
    errors: tuple[str, ...]
    """The errors of the run. A URL loses its credentials, and each error is cut."""


class RunPage(BaseModel):
    """One page of runs, newest first, and the cursor of the next page."""

    items: tuple[RunSummary, ...]
    next_cursor: str | None


class RunDiff(BaseModel):
    """The remediation, the rollback, and the future config, as config lines."""

    remediation: tuple[str, ...]
    rollback: tuple[str, ...]
    future: tuple[str, ...]


type _Row = tuple[models.RemediationRun, models.Device, str]


def runs_router(settings: CoreSettings, deps: ApiDeps) -> APIRouter:
    """Return the router of `/runs`."""
    router = APIRouter(prefix="/runs", tags=["runs"])

    @router.post("", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(  # pyright: ignore[reportUnusedFunction]
        body: CreateRun,
        actor: Annotated[Actor, Depends(current_actor)],
        response: Response,
        background: BackgroundTasks,
    ) -> RunCreated:
        """Create a change set and a run, then start the review graph after the response."""
        device = await find_device(deps, body.device_id)
        runs = deps.review.runs
        change_set_id = await runs.create_change_set(
            body.title or f"run on {device.name}", actor
        )
        thread_id = new_id()
        run_id = await runs.create_run(change_set_id, device.id, thread_id, actor)
        state: ReviewState = {
            "run_id": run_id,
            "change_set_id": change_set_id,
            "requested_by": actor.name,
        }
        background.add_task(_start, settings, deps, state, thread_id)
        response.headers["Location"] = f"{API_PREFIX}{router.prefix}/{run_id}"
        return RunCreated(
            run_id=run_id, change_set_id=change_set_id, state=RunState.DRAFT
        )

    @router.get("")
    async def list_runs(  # pyright: ignore[reportUnusedFunction]
        limit: Limit = DEFAULT_PAGE,
        cursor: Cursor = None,
        state: Annotated[RunState | None, Query()] = None,
    ) -> RunPage:
        """Return one page of runs, newest first, in one state or in all."""
        query = _runs().order_by(models.RemediationRun.id.desc()).limit(limit + 1)
        if cursor is not None:
            query = query.where(models.RemediationRun.id < cursor)
        if state is not None:
            query = query.where(models.RemediationRun.state == state)
        async with deps.sessions() as session:
            rows = (await session.execute(query)).all()
        items = tuple(starmap(_summary, rows[:limit]))
        return RunPage(
            items=items, next_cursor=items[-1].id if len(rows) > limit else None
        )

    @router.get("/{run_id}")
    async def show_run(run_id: str) -> RunDetail:  # pyright: ignore[reportUnusedFunction]
        """Return the run with its state, its tiers, its errors, and its times."""
        row = await _run(deps, run_id)
        run = row[0]
        return RunDetail(
            **_summary(*row).model_dump(),
            plan_source=run.plan_source,
            change_fingerprint=run.change_fingerprint,
            errors=_errors(run.error),
        )

    @router.get("/{run_id}/diff")
    async def show_diff(run_id: str) -> RunDiff:  # pyright: ignore[reportUnusedFunction]
        """Return the remediation, the rollback, and the future, rendered by the engine.

        Raises:
            RunNotReadyError: If the run has no remediation yet.

        """
        run, device, _ = await _run(deps, run_id)
        async with deps.sessions() as session:
            future = (
                None
                if run.future_snapshot_id is None
                else await session.get(models.ConfigSnapshot, run.future_snapshot_id)
            )
        if future is None or run.remediation_dump is None or run.rollback_dump is None:
            message = f"run {run_id!r} is {run.state}; it has no remediation yet"
            raise RunNotReadyError(message)
        dumps = (run.remediation_dump, run.rollback_dump, future.dump)
        remediation, rollback, rendered = await asyncio.to_thread(
            _render, device.platform_name, dumps
        )
        return RunDiff(remediation=remediation, rollback=rollback, future=rendered)

    return router


def _render(
    platform_name: str, dumps: tuple[models.JSONObject, ...]
) -> tuple[tuple[str, ...], ...]:
    return tuple(render_lines(platform_name, dump) for dump in dumps)


async def _start(
    settings: CoreSettings, deps: ApiDeps, state: ReviewState, thread_id: str
) -> None:
    try:
        async with open_checkpointer(settings) as saver:
            graph = build_review_graph(deps.review, assessment=deps.assessment).compile(  # pyright: ignore[reportUnknownMemberType]
                checkpointer=saver
            )
            await start_run(graph, state, thread_id)
    except Exception as error:  # ruff: ignore[blind-except]
        _log.error("run_failed", run_id=state.get("run_id"), error=type(error).__name__)


def _runs() -> Select[models.RemediationRun, models.Device, str]:
    return (
        select(models.RemediationRun, models.Device, models.ChangeSet.requested_by)
        .join(models.Device, models.RemediationRun.device_id == models.Device.id)
        .join(
            models.ChangeSet, models.RemediationRun.change_set_id == models.ChangeSet.id
        )
    )


async def _run(deps: ApiDeps, run_id: str) -> _Row:
    async with deps.sessions() as session:
        row = (
            await session.execute(_runs().where(models.RemediationRun.id == run_id))
        ).one_or_none()
    if row is None:
        message = f"no remediation run {run_id!r}"
        raise RunNotFoundError(message)
    run, device, requested_by = row
    return run, device, requested_by


def _summary(
    run: models.RemediationRun, device: models.Device, requested_by: str
) -> RunSummary:
    return RunSummary(
        id=run.id,
        change_set_id=run.change_set_id,
        device_id=device.id,
        sot_device_id=device.sot_device_id,
        device_name=device.name,
        state=RunState(run.state),
        requested_by=requested_by,
        risk_tier_floor=run.risk_tier_floor,
        risk_tier_final=run.risk_tier_final,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _errors(error: models.JSONObject | None) -> tuple[str, ...]:
    listed = (error or {}).get("errors")
    if not isinstance(listed, list):
        return ()
    items = cast("list[object]", listed)
    return tuple(
        _URL_CREDENTIALS.sub(r"\g<scheme>***@", str(item))[:MAX_ERROR_CHARACTERS]
        for item in items[:MAX_ERRORS]
    )
