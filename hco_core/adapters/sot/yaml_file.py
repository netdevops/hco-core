"""A source of truth in one YAML file, for development and tests."""

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml
from pydantic import ConfigDict, Field, JsonValue, ValidationError

from hco_core.adapters.errors import DeviceNotFoundError, InvalidInventoryError
from hco_core.domain.base import BaseModel
from hco_core.domain.config import ConfigDocument
from hco_core.domain.device import (
    DevicePage,
    DeviceRecord,
    DeviceSelector,
    TopologyEdge,
    TopologyGraph,
)
from hco_core.domain.ids import DeviceID
from hco_core.domain.observability import HealthStatus
from hco_core.domain.secrets import SecretPurpose, SecretRef
from hco_core.engine.errors import UnknownPlatformError
from hco_core.engine.platforms import canonical_platform_name

MAX_INVENTORY_BYTES = 1024 * 1024
"""The largest inventory file that the adapter reads."""
MAX_INVENTORY_DEPTH = 32
"""The deepest nesting of mappings and lists in an inventory file."""
_OPENS = (yaml.MappingStartEvent, yaml.SequenceStartEvent)
_CLOSES = (yaml.MappingEndEvent, yaml.SequenceEndEvent)


class _Entry(BaseModel):
    """One device in the file: the `DeviceRecord` fields and four file keys."""

    model_config = ConfigDict(extra="allow")

    intended_config: str | None = None
    config_context: dict[str, JsonValue] = Field(default_factory=dict)
    secret: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class _Topology(BaseModel):
    edges: tuple[TopologyEdge, ...] = ()
    redundancy_groups: dict[str, tuple[DeviceID, ...]] = Field(default_factory=dict)


class _File(BaseModel):
    devices: tuple[_Entry, ...] = ()
    topology: _Topology = _Topology()


@dataclass(frozen=True)
class _Inventory:
    records: dict[DeviceID, DeviceRecord]
    entries: dict[DeviceID, _Entry]
    topology: _Topology

    def entry(self, device_id: DeviceID) -> _Entry:
        if device_id not in self.entries:
            message = f"the inventory has no device {device_id!r}"
            raise DeviceNotFoundError(message)
        return self.entries[device_id]


class YamlSourceOfTruth:
    """Read devices, intended configs, and topology from one YAML file.

    The file holds `devices`, a list of `DeviceRecord` fields with the extra
    keys `intended_config`, `config_context`, and `secret`, and `topology`,
    with `edges` and `redundancy_groups`. The adapter reads the file on each
    call. `intended_config` is a path inside the folder of the file.
    """

    def __init__(self, path: Path) -> None:
        """Use the inventory file at the path."""
        self._path = path

    @property
    def name(self) -> str:
        """The adapter name, `yaml`."""
        return "yaml"

    async def health(self) -> HealthStatus:
        """Report whether the file is readable and valid."""
        checked_at = datetime.now(UTC)
        try:
            inventory = await self._load()
        except (OSError, InvalidInventoryError) as error:
            detail = f"{self._path}: {error}"
            return HealthStatus(healthy=False, detail=detail, checked_at=checked_at)
        detail = f"{len(inventory.records)} devices in {self._path}"
        return HealthStatus(healthy=True, detail=detail, checked_at=checked_at)

    async def list_devices(
        self,
        *,
        selector: DeviceSelector | None = None,
        cursor: str | None = None,
        limit: int = 500,
    ) -> DevicePage:
        """Return one page of the devices that the selector matches, in file order.

        The cursor is the offset of the next page.

        Raises:
            ValueError: If the cursor is not a non-negative integer, or the
                limit is below 1.

        """
        if (cursor is not None and not cursor.isdigit()) or limit < 1:
            message = f"the cursor {cursor!r} or the limit {limit} is not valid"
            raise ValueError(message)
        inventory = await self._load()
        matched = [
            record
            for record in inventory.records.values()
            if selector is None or _selected(record, selector)
        ]
        start = int(cursor or 0)
        end = start + limit
        return DevicePage(
            devices=tuple(matched[start:end]),
            cursor=str(end) if end < len(matched) else None,
            total=len(matched),
        )

    async def get_device(self, device_id: DeviceID) -> DeviceRecord:
        """Return the device with the id."""
        inventory = await self._load()
        inventory.entry(device_id)
        return inventory.records[device_id]

    async def intended_config(self, device_id: DeviceID) -> ConfigDocument | None:
        """Return the text of the device's `intended_config` file, or None."""
        return await asyncio.to_thread(self._intended_config, device_id)

    async def config_context(self, device_id: DeviceID) -> Mapping[str, object]:
        """Return the device's `config_context` mapping."""
        return (await self._load()).entry(device_id).config_context

    async def topology(self, device_ids: Sequence[DeviceID]) -> TopologyGraph:
        """Return the edges and the redundancy groups that hold one of the devices."""
        wanted = set(device_ids)
        topology = (await self._load()).topology
        return TopologyGraph(
            nodes=tuple(device_ids),
            edges=tuple(edge for edge in topology.edges if {edge.a, edge.b} & wanted),
            redundancy_groups={
                group: members
                for group, members in topology.redundancy_groups.items()
                if wanted.intersection(members)
            },
        )

    async def secret_ref(
        self, device_id: DeviceID, purpose: SecretPurpose
    ) -> SecretRef:
        """Return an `env` reference with the device's `secret` key, or its id."""
        entry = (await self._load()).entry(device_id)
        return SecretRef(provider="env", key=entry.secret or device_id, purpose=purpose)

    async def _load(self) -> _Inventory:
        return await asyncio.to_thread(_load, self._path)

    def _intended_config(self, device_id: DeviceID) -> ConfigDocument | None:
        inventory = _load(self._path)
        entry = inventory.entry(device_id)
        if entry.intended_config is None:
            return None
        record = inventory.records[device_id]
        folder = self._path.parent.resolve()
        path = (folder / entry.intended_config).resolve()
        where = f"device {record.name!r}, key 'intended_config'"
        if not path.is_relative_to(folder):
            message = f"{where}: {entry.intended_config} is outside {folder}"
            raise InvalidInventoryError(message)
        try:
            body = path.read_text(encoding="utf8")
        except (OSError, UnicodeDecodeError) as error:
            message = (
                f"{where}: cannot read {entry.intended_config} as UTF-8 text: {error}"
            )
            raise InvalidInventoryError(message) from error
        return ConfigDocument(
            body=body,
            format=record.config_format,
            collected_at=datetime.now(UTC),
            source="sot",
        )


def _load(path: Path) -> _Inventory:
    """Read and check the inventory file. An unreadable file raises `OSError`.

    Raises:
        InvalidInventoryError: If the file is not a valid inventory.

    """
    if path.stat().st_size > MAX_INVENTORY_BYTES:
        message = f"the inventory is larger than {MAX_INVENTORY_BYTES} bytes"
        raise InvalidInventoryError(message)
    try:
        text = path.read_text(encoding="utf8")
        _check_structure(text)
        data = _File.model_validate(yaml.safe_load(text) or {})
    except UnicodeDecodeError as error:
        message = "the inventory is not UTF-8 text"
        raise InvalidInventoryError(message) from error
    except yaml.YAMLError as error:
        message = f"the inventory is not valid YAML: {_yaml_problem(error)}"
        raise InvalidInventoryError(message) from error
    except ValidationError as error:
        message = f"the inventory must be a mapping of devices and topology: {_problems(error)}"
        raise InvalidInventoryError(message) from error
    records: dict[DeviceID, DeviceRecord] = {}
    entries: dict[DeviceID, _Entry] = {}
    for entry in data.devices:
        record = _record(entry)
        if record.id in records:
            message = f"the device id {record.id!r} is in the inventory more than once"
            raise InvalidInventoryError(message)
        records[record.id] = record
        entries[record.id] = entry
    return _Inventory(records=records, entries=entries, topology=data.topology)


def _check_structure(text: str) -> None:
    """Refuse YAML aliases and nesting deeper than `MAX_INVENTORY_DEPTH`.

    The scan stops at the first problem, so a hostile file costs little.

    Raises:
        InvalidInventoryError: For an alias or for nesting that is too deep.

    """
    depth = 0
    events = cast("Iterator[object]", yaml.parse(text, Loader=yaml.SafeLoader))  # pyright: ignore[reportUnknownMemberType]
    for event in events:
        if isinstance(event, yaml.AliasEvent):
            message = "the inventory must not use YAML anchors and aliases"
            raise InvalidInventoryError(message)
        depth += isinstance(event, _OPENS) - isinstance(event, _CLOSES)
        if depth > MAX_INVENTORY_DEPTH:
            message = f"the inventory is nested more than {MAX_INVENTORY_DEPTH} levels"
            raise InvalidInventoryError(message)


def _record(entry: _Entry) -> DeviceRecord:
    """Return the device record of the entry, with a known platform name.

    Raises:
        InvalidInventoryError: If a field or the platform name is not valid.

    """
    fields = dict(entry.model_extra or {})
    device = repr(fields.get("name", fields.get("id")))
    metadata: dict[str, str] = {}
    for key, value in entry.metadata.items():
        if isinstance(value, dict | list):
            message = (
                f"device {device}, metadata key {key!r}: the value must be a scalar"
            )
            raise InvalidInventoryError(message)
        metadata[key] = str(value)
    try:
        record = DeviceRecord.model_validate({**fields, "metadata": metadata})
    except ValidationError as error:
        message = f"device {device}: {_problems(error)}"
        raise InvalidInventoryError(message) from error
    try:
        canonical = canonical_platform_name(record.platform_name)
    except UnknownPlatformError as error:
        message = f"device {device}: unknown platform_name {record.platform_name!r}"
        raise InvalidInventoryError(message) from error
    if canonical != record.platform_name:
        message = f"device {device}: unknown platform_name {record.platform_name!r}; use {canonical}"
        raise InvalidInventoryError(message)
    return record


def _yaml_problem(error: yaml.YAMLError) -> str:
    """Return the problem and its line and column, never a part of the file."""
    if not isinstance(error, yaml.MarkedYAMLError):
        return type(error).__name__
    problems = "; ".join(part for part in (error.context, error.problem) if part)
    mark = error.problem_mark
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    return f"{problems}{where}"


def _problems(error: ValidationError) -> str:
    """Return the place and the problem of each error, never the input value."""
    return "; ".join(
        f"{'.'.join(map(str, problem['loc']))}: {problem['msg']}"
        for problem in error.errors(include_input=False, include_url=False)
    )


def _selected(record: DeviceRecord, selector: DeviceSelector) -> bool:
    """Return True if the record matches every field of the selector."""
    fields = (
        (selector.ids, record.id),
        (selector.names, record.name),
        (selector.platforms, record.platform_name),
        (selector.roles, record.role),
        (selector.sites, record.site),
    )
    return (
        all(not wanted or value in wanted for wanted, value in fields)
        and (not selector.tags_any or bool(selector.tags_any & record.tags))
        and selector.tags_all <= record.tags
    )
