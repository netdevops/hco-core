"""The device routes: sync the source of truth into the `device` table, and read it."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import Insert, insert

from hco_core.adapters.errors import DeviceNotFoundError
from hco_core.api.actor import current_actor
from hco_core.api.deps import DEFAULT_PAGE, ApiDeps, Cursor, Limit
from hco_core.db import models
from hco_core.db.types import new_id
from hco_core.domain.base import BaseModel
from hco_core.domain.device import DeviceRecord
from hco_core.ports import SourceOfTruth
from hco_core.services.audit import Actor, AuditEntry, add_audit_event

_UNIQUE_DEVICE = "uq_device_tenant_id_sot_name_sot_device_id"


class Device(BaseModel):
    """One device row, as the last sync wrote it."""

    id: str
    sot_name: str
    sot_device_id: str
    name: str
    platform_name: str
    mgmt_address: str
    role: str | None
    site: str | None
    criticality: str
    tags: tuple[str, ...]
    in_maintenance: bool
    metadata: dict[str, str]
    last_synced_at: datetime


class DevicePage(BaseModel):
    """One page of devices, and the cursor of the next page."""

    items: tuple[Device, ...]
    next_cursor: str | None


class SyncResult(BaseModel):
    """How many devices the sync wrote."""

    synced: int


def devices_router(deps: ApiDeps) -> APIRouter:
    """Return the router of `/devices`."""
    router = APIRouter(prefix="/devices", tags=["devices"])
    sot = deps.review.sot

    @router.get("")
    async def list_devices(  # pyright: ignore[reportUnusedFunction]
        limit: Limit = DEFAULT_PAGE, cursor: Cursor = None
    ) -> DevicePage:
        """Return one page of devices, in the order of the first sync."""
        query = select(models.Device).order_by(models.Device.id).limit(limit + 1)
        if cursor is not None:
            query = query.where(models.Device.id > cursor)
        async with deps.sessions() as session:
            rows = (await session.execute(query)).scalars().all()
        items = tuple(_device(row) for row in rows[:limit])
        return DevicePage(
            items=items, next_cursor=items[-1].id if len(rows) > limit else None
        )

    @router.get("/{device_id}")
    async def show_device(device_id: str) -> Device:  # pyright: ignore[reportUnusedFunction]
        """Return the device with the row id or the source of truth id."""
        return _device(await find_device(deps, device_id))

    @router.post("/sync")
    async def sync_devices(  # pyright: ignore[reportUnusedFunction]
        actor: Annotated[Actor, Depends(current_actor)],
    ) -> SyncResult:
        """Write every device of the source of truth. A known device is updated."""
        records = await _all_devices(sot)
        async with deps.sessions.begin() as session:
            if records:
                await session.execute(_upsert(sot.name, records))
            add_audit_event(
                session,
                AuditEntry(
                    actor=actor,
                    action="device.sync",
                    object_type="device",
                    object_id=sot.name,
                    after={"count": len(records)},
                ),
            )
        return SyncResult(synced=len(records))

    return router


async def find_device(deps: ApiDeps, device_id: str) -> models.Device:
    """Return the device with the row id, or with the id in the source of truth.

    Raises:
        DeviceNotFoundError: If no device has the id.

    """
    query = (
        select(models.Device)
        .where(
            or_(
                models.Device.id == device_id,
                (models.Device.sot_name == deps.review.sot.name)
                & (models.Device.sot_device_id == device_id),
            )
        )
        .order_by(models.Device.id != device_id)
        .limit(1)
    )
    async with deps.sessions() as session:
        device = (await session.execute(query)).scalar_one_or_none()
    if device is None:
        message = (
            f"no device {device_id!r}; POST /api/v1/devices/sync reads the "
            "source of truth"
        )
        raise DeviceNotFoundError(message)
    return device


async def _all_devices(sot: SourceOfTruth) -> list[DeviceRecord]:
    records: list[DeviceRecord] = []
    cursor: str | None = None
    while True:
        page = await sot.list_devices(cursor=cursor)
        records.extend(page.devices)
        cursor = page.cursor
        if cursor is None:
            return records


def _upsert(sot_name: str, records: list[DeviceRecord]) -> Insert:
    values = [
        {
            "id": new_id(),
            "sot_name": sot_name,
            "sot_device_id": record.id,
            "name": record.name,
            "platform_name": record.platform_name,
            "mgmt_address": record.mgmt_address,
            "role": record.role,
            "site": record.site,
            "criticality": record.criticality,
            "tags": sorted(record.tags),
            "config_format": record.config_format.value,
            "in_maintenance": record.in_maintenance,
            "metadata_": dict(record.metadata),
        }
        for record in records
    ]
    statement = insert(models.Device).values(values)
    updated = {
        key: statement.excluded[key]
        for key in (
            "name",
            "platform_name",
            "mgmt_address",
            "role",
            "site",
            "criticality",
            "tags",
            "config_format",
            "in_maintenance",
            "metadata",
        )
    }
    return statement.on_conflict_do_update(
        constraint=_UNIQUE_DEVICE,
        set_={**updated, "last_synced_at": func.now(), "updated_at": func.now()},
    )


def _device(row: models.Device) -> Device:
    return Device(
        id=row.id,
        sot_name=row.sot_name,
        sot_device_id=row.sot_device_id,
        name=row.name,
        platform_name=row.platform_name,
        mgmt_address=row.mgmt_address,
        role=row.role,
        site=row.site,
        criticality=row.criticality,
        tags=tuple(row.tags),
        in_maintenance=row.in_maintenance,
        metadata={key: str(value) for key, value in row.metadata_.items()},
        last_synced_at=row.last_synced_at,
    )
