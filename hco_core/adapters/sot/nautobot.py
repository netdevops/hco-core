"""A source of truth in Nautobot, read over GraphQL and REST.

The query shapes target Nautobot 2.1 to 2.4 and the Golden Config app 2.x.
The adapter only reads: each request is a GET, or a POST of a GraphQL query.
"""

import asyncio
import errno
import ipaddress
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Final, cast

import httpx
import structlog
from pydantic import (
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    ValidationError,
    field_validator,
)

from hco_core.adapters.errors import (
    AdapterConfigError,
    AdapterError,
    DeviceNotFoundError,
    InvalidInventoryError,
    SourceOfTruthUnreachableError,
)
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
from hco_core.engine.platforms import platform_name_of_mapping

MAX_PAGE_SIZE: Final = 500
"""The most devices that one GraphQL query asks for."""
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
"""The largest answer that the adapter reads, after decompression."""
MAX_DEPTH: Final = 32
"""The deepest nesting of objects and lists in an answer."""
DEFAULT_REDUNDANCY_TAG_PREFIX: Final = "hco-redundancy-"
"""A tag with this prefix puts a device in the redundancy group of the rest."""
GRAPHQL_PATH: Final = "/api/graphql/"
GOLDEN_CONFIG_PATH: Final = "/api/plugins/golden-config/golden-config/"
STATUS_PATH: Final = "/api/status/"
DEVICES_QUERY: Final = """query HcoDevices($limit: Int, $offset: Int, $id: [String]) {
  devices(limit: $limit, offset: $offset, id: $id) {
    id
    name
    platform { network_driver_mappings }
    role { name }
    location { name }
    tags { name }
    primary_ip4 { host }
    interfaces { name cable_peer_interface { name device { id } } }
  }
}"""
CONTEXT_QUERY: Final = """query HcoContext($id: [String]) {
  devices(id: $id) { id name config_context }
}"""
GROUP_QUERY: Final = """query HcoGroup($tags: [String]) {
  devices(tags: $tags) { id }
}"""
CREDENTIAL_KEY_FIELD: Final = "hco_secret_key"
"""The config context key that names the `env` secret key of a device."""
MAX_SECRET_KEY_LENGTH: Final = 128
MAX_KEY_LENGTH: Final = 255
"""The longest key of a skipped device. A longer name is cut."""
_NOT_KEY_CHARACTERS = re.compile(r"[^A-Z0-9_]")
NO_PLATFORM: Final = "the device has no platform"
UNMAPPED_PLATFORM: Final = "the platform has no hier_config driver mapping"
NO_ADDRESS: Final = "the device has no primary IPv4 address"
BAD_ADDRESS: Final = "the primary IPv4 address is not valid"
CONTROL_CHARACTER: Final = (
    "a name, a role, a location, or a tag holds a control character"
)
_JSON_TOKENS = re.compile(rb'["\\\[\]{}]')
_QUOTE, _BACKSLASH = ord('"'), ord("\\")
_OPENERS, _CLOSERS = frozenset(b"[{"), frozenset(b"]}")
_log = structlog.get_logger(__name__)


class _Answer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Named(_Answer):
    name: str


class _Ref(_Answer):
    id: str


class _Platform(_Answer):
    network_driver_mappings: dict[str, str | None] = Field(default_factory=dict)

    @field_validator("network_driver_mappings", mode="before")
    @classmethod
    def _decode(cls, value: object) -> object:
        """Accept the mappings as a JSON object or as JSON text.

        Raises:
            ValueError: If the JSON text is nested too deeply.

        """
        if not isinstance(value, str):
            return value
        try:
            return cast("object", json.loads(value))
        except RecursionError as error:
            message = "the mappings are nested too deeply"
            raise ValueError(message) from error


class _Peer(_Answer):
    name: str
    device: _Ref


class _Interface(_Answer):
    name: str
    cable_peer_interface: _Peer | None = None


class _Address(_Answer):
    host: str


class _Device(_Answer):
    id: str
    name: str | None = None
    platform: _Platform | None = None
    role: _Named | None = None
    location: _Named | None = None
    tags: tuple[_Named, ...] = ()
    primary_ip4: _Address | None = None
    interfaces: tuple[_Interface, ...] = ()
    config_context: dict[str, JsonValue] | None = None


class _Devices(_Answer):
    devices: tuple[_Device, ...] | None = None


class _GraphQL(_Answer):
    data: _Devices | None = None
    errors: tuple[JsonValue, ...] | None = None


class _GoldenConfig(_Answer):
    device: _Ref | None = None
    intended_config: str | None = None


class _GoldenConfigs(_Answer):
    results: tuple[_GoldenConfig, ...] = ()


_MODELS = (_Named, _Ref, _Platform, _Peer, _Interface, _Address, _Device, _Devices)
_FIELD_NAMES = frozenset(
    name
    for model in (*_MODELS, _GraphQL, _GoldenConfig, _GoldenConfigs)
    for name in model.model_fields
)


class NautobotSourceOfTruth:
    """Read devices, intended configs, and topology from Nautobot.

    One GraphQL query reads each page of devices. The platform name comes from
    `platform.network_driver_mappings["hier_config"]`. A device with no usable
    platform or address is left out and named in `DevicePage.skipped`, never
    treated as GENERIC. The intended config comes from the Golden Config app.
    """

    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        url: str,
        token: SecretStr,
        *,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        redundancy_tag_prefix: str = DEFAULT_REDUNDANCY_TAG_PREFIX,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Use the Nautobot at the URL. A user name and a password in the URL are dropped.

        Raises:
            AdapterConfigError: If the URL is not an http or https URL, or the
                token is empty or not printable ASCII.

        """
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            parsed = httpx.URL()
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            message = "the Nautobot URL must start with http or https and name a host"
            raise AdapterConfigError(message)
        secret = token.get_secret_value()
        if not (secret.strip() and secret.isascii() and secret.isprintable()):
            message = (
                "the Nautobot token must be printable ASCII and not empty; "
                "check HCO_NAUTOBOT_TOKEN"
            )
            raise AdapterConfigError(message)
        bare = parsed.copy_with(userinfo=b"", query=None, fragment=None)
        self._url = str(bare).rstrip("/")
        if not verify_ssl:
            _log.warning("nautobot_tls_verification_disabled", url=self._url)
        self._token = token
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._prefix = redundancy_tag_prefix
        self._transport = transport

    def __repr__(self) -> str:
        """Show the URL without a user name or a password, and never the token."""
        return f"NautobotSourceOfTruth(url={self._url!r})"

    @property
    def name(self) -> str:
        """The adapter name, `nautobot`."""
        return "nautobot"

    async def health(self) -> HealthStatus:
        """Report whether Nautobot answers and accepts the token."""
        checked_at = datetime.now(UTC)
        try:
            await self._request("GET", STATUS_PATH)
        except AdapterError as error:
            return HealthStatus(healthy=False, detail=str(error), checked_at=checked_at)
        detail = f"Nautobot at {self._url} answers"
        return HealthStatus(healthy=True, detail=detail, checked_at=checked_at)

    async def list_devices(
        self,
        *,
        selector: DeviceSelector | None = None,
        cursor: str | None = None,
        limit: int = 500,
    ) -> DevicePage:
        """Return the devices of one Nautobot page that the selector matches.

        The cursor is the Nautobot offset of the next page. A page holds at
        most `MAX_PAGE_SIZE` devices, and the selector applies after the query.

        Raises:
            ValueError: If the cursor is not a non-negative integer, or the
                limit is below 1.

        """
        if (
            cursor is not None and not (cursor.isascii() and cursor.isdigit())
        ) or limit < 1:
            message = f"the cursor {cursor!r} or the limit {limit} is not valid"
            raise ValueError(message)
        offset = int(cursor or 0)
        size = min(limit, MAX_PAGE_SIZE)
        devices = await self._devices(
            DEVICES_QUERY, {"limit": size + 1, "offset": offset}
        )
        records: list[DeviceRecord] = []
        skipped: dict[str, str] = {}
        for device in devices[:size]:
            record = _record(device)
            if isinstance(record, str):
                key = _skip_key(device)
                skipped[key] = record
                _log.warning("device_skipped", device=key, reason=record)
            elif selector is None or selector.matches(record):
                records.append(record)
        return DevicePage(
            devices=tuple(records),
            cursor=str(offset + size) if len(devices) > size else None,
            skipped=skipped,
        )

    async def get_device(self, device_id: DeviceID) -> DeviceRecord:
        """Return the device with the id.

        Raises:
            InvalidInventoryError: If the device has no usable platform or address.

        """
        record = _record(await self._one(DEVICES_QUERY, device_id))
        if isinstance(record, str):
            message = f"device {device_id!r}: {record}"
            raise InvalidInventoryError(message)
        return record

    async def intended_config(self, device_id: DeviceID) -> ConfigDocument | None:
        """Return the Golden Config intended config of the device, or None.

        Raises:
            InvalidInventoryError: If a row is not of the device, or the device
                has more than one row.

        """
        _check_id(device_id)
        body = await self._request(
            "GET", GOLDEN_CONFIG_PATH, params={"device_id": device_id}
        )
        rows = (await asyncio.to_thread(_parse, body, _GoldenConfigs)).results
        others = sum(
            row.device is None or not _same_id(row.device.id, device_id) for row in rows
        )
        if others or len(rows) > 1:
            message = (
                f"the Golden Config answer for device {device_id!r} has {len(rows)} "
                f"rows, and {others} of them are of another device; expected one row"
            )
            raise InvalidInventoryError(message)
        if not rows or not rows[0].intended_config:
            return None
        return ConfigDocument(
            body=rows[0].intended_config, collected_at=datetime.now(UTC), source="sot"
        )

    async def config_context(self, device_id: DeviceID) -> Mapping[str, object]:
        """Return the rendered config context of the device."""
        return (await self._one(CONTEXT_QUERY, device_id)).config_context or {}

    async def topology(self, device_ids: Sequence[DeviceID]) -> TopologyGraph:
        """Return the cables of the devices, and each redundancy group of a device.

        A tag that starts with the redundancy prefix names a group. The group
        holds every device in Nautobot with that tag.
        """
        wanted = list(dict.fromkeys(device_ids))
        for device_id in wanted:
            _check_id(device_id)
        devices = await self._devices(DEVICES_QUERY, {"id": wanted}) if wanted else ()
        edges: dict[frozenset[tuple[str, str]], TopologyEdge] = {}
        groups: set[str] = set()
        for device in devices:
            groups.update(
                tag.name
                for tag in device.tags
                if tag.name.startswith(self._prefix) and tag.name != self._prefix
            )
            for interface in device.interfaces:
                peer = interface.cable_peer_interface
                if peer is None:
                    continue
                ends = frozenset(
                    {(device.id, interface.name), (peer.device.id, peer.name)}
                )
                edges.setdefault(
                    ends,
                    TopologyEdge(
                        a=DeviceID(device.id),
                        b=DeviceID(peer.device.id),
                        a_interface=interface.name,
                        b_interface=peer.name,
                    ),
                )
        redundancy_groups: dict[str, tuple[DeviceID, ...]] = {}
        for tag in sorted(groups):
            members = await self._devices(GROUP_QUERY, {"tags": [tag]})
            redundancy_groups[tag.removeprefix(self._prefix)] = tuple(
                DeviceID(member.id) for member in members
            )
        return TopologyGraph(
            nodes=tuple(device_ids),
            edges=tuple(edges.values()),
            redundancy_groups=redundancy_groups,
        )

    async def secret_ref(
        self, device_id: DeviceID, purpose: SecretPurpose
    ) -> SecretRef:
        """Return an `env` reference to the credential of the device, never a value.

        The key is `hco_secret_key` of the config context. Without it, the key
        is the device name in upper case, with each character other than a
        letter, a digit, or `_` changed to `_`.

        Raises:
            InvalidInventoryError: If `hco_secret_key` is not a non-empty
                string of `MAX_SECRET_KEY_LENGTH` characters or fewer.

        """
        device = await self._one(CONTEXT_QUERY, device_id)
        context = device.config_context or {}
        if CREDENTIAL_KEY_FIELD not in context:
            name = (device.name or device.id).upper()
            key = _NOT_KEY_CHARACTERS.sub("_", name)
            return SecretRef(provider="env", key=key, purpose=purpose)
        value = context[CREDENTIAL_KEY_FIELD]
        if not (
            isinstance(value, str)
            and value.strip()
            and len(value) <= MAX_SECRET_KEY_LENGTH
        ):
            message = (
                f"device {device_id!r}, config_context key {CREDENTIAL_KEY_FIELD!r}: "
                f"the value must be text of 1 to {MAX_SECRET_KEY_LENGTH} characters"
            )
            raise InvalidInventoryError(message)
        return SecretRef(provider="env", key=value, purpose=purpose)

    async def _one(self, query: str, device_id: DeviceID) -> _Device:
        _check_id(device_id)
        for device in await self._devices(query, {"id": [device_id]}):
            if device.id == device_id:
                return device
        message = f"Nautobot has no device {device_id!r}"
        raise DeviceNotFoundError(message)

    async def _devices(
        self, query: str, variables: Mapping[str, object]
    ) -> tuple[_Device, ...]:
        body = await self._request(
            "POST", GRAPHQL_PATH, payload={"query": query, "variables": variables}
        )
        answer = await asyncio.to_thread(_parse, body, _GraphQL)
        if answer.errors or answer.data is None or answer.data.devices is None:
            message = (
                f"Nautobot refused the query with {len(answer.errors or ())} errors; "
                "hco-core needs Nautobot 2.x"
            )
            raise InvalidInventoryError(message)
        return answer.data.devices

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> bytes:
        """Return the body of a 2xx answer, read up to `MAX_RESPONSE_BYTES`.

        Raises:
            SourceOfTruthUnreachableError: If Nautobot does not answer, or
                answers with a status other than 2xx.

        """
        headers = {
            "Authorization": f"Token {self._token.get_secret_value()}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(
            base_url=self._url,
            headers=headers,
            verify=self._verify_ssl,
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            try:
                async with client.stream(
                    method, path, params=params, json=payload
                ) as response:
                    self._check_status(path, response.status_code)
                    return await _bounded(response)
            except httpx.HTTPError as error:
                message = (
                    f"Nautobot at {self._url} does not answer "
                    f"({type(error).__name__}); check HCO_NAUTOBOT_URL"
                )
                raise SourceOfTruthUnreachableError(
                    message, _transport_errno(error)
                ) from None

    def _check_status(self, path: str, status: int) -> None:
        if status in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
            message = (
                f"Nautobot at {self._url} refused the token with HTTP {status}; "
                "check HCO_NAUTOBOT_TOKEN"
            )
            raise SourceOfTruthUnreachableError(message, errno.EACCES)
        if status == HTTPStatus.NOT_FOUND and path == GOLDEN_CONFIG_PATH:
            message = (
                f"the Golden Config app does not answer at {self._url}"
                f"{GOLDEN_CONFIG_PATH} (HTTP 404); install the app in Nautobot"
            )
            raise SourceOfTruthUnreachableError(message, errno.ENOENT)
        if not httpx.codes.is_success(status):
            message = (
                f"Nautobot at {self._url} answered HTTP {status}; "
                "check HCO_NAUTOBOT_URL"
            )
            code = errno.ENOENT if status == HTTPStatus.NOT_FOUND else errno.EPROTO
            raise SourceOfTruthUnreachableError(message, code)


def _transport_errno(error: httpx.HTTPError) -> int:
    """Return the errno of a transport error, for the reason in the log."""
    if isinstance(error, httpx.TimeoutException):
        return errno.ETIMEDOUT
    if isinstance(error, httpx.ConnectError):
        return errno.ECONNREFUSED
    return errno.EIO


async def _bounded(response: httpx.Response) -> bytes:
    """Return the body, and stop at `MAX_RESPONSE_BYTES`.

    Raises:
        InvalidInventoryError: If the body is larger.

    """
    too_large = f"the Nautobot answer is larger than {MAX_RESPONSE_BYTES} bytes"
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
        raise InvalidInventoryError(too_large)
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise InvalidInventoryError(too_large)
    return bytes(body)


def _parse[M: _Answer](body: bytes, model: type[M]) -> M:
    """Return the answer as the model. An error quotes nothing from the answer.

    Raises:
        InvalidInventoryError: If the answer is not UTF-8 JSON of the shape,
            or is nested more than `MAX_DEPTH` levels.

    """
    nested = f"the Nautobot answer is nested more than {MAX_DEPTH} levels"
    if _too_deep(body):
        raise InvalidInventoryError(nested)
    try:
        value = cast("object", json.loads(body.decode("utf8")))
    except UnicodeDecodeError:
        message = "the Nautobot answer is not UTF-8 text"
        raise InvalidInventoryError(message) from None
    except ValueError:
        message = "the Nautobot answer is not valid JSON"
        raise InvalidInventoryError(message) from None
    try:
        return model.model_validate(value)
    except ValidationError as error:
        message = (
            f"the Nautobot answer does not have the expected shape: {_problems(error)}"
        )
        raise InvalidInventoryError(message) from None


def _too_deep(body: bytes) -> bool:
    """Return True if the JSON text nests lists and objects past `MAX_DEPTH`.

    One scan reads each quote, backslash, and bracket once, and stops at the
    first level past the limit. It does not depend on the depth limit of `json`.
    """
    depth = 0
    in_string = False
    escaped = -1
    for token in _JSON_TOKENS.finditer(body):
        position = token.start()
        character = body[position]
        if position == escaped:
            continue
        if in_string:
            if character == _BACKSLASH:
                escaped = position + 1
            in_string = character != _QUOTE
        elif character == _QUOTE:
            in_string = True
        elif character in _OPENERS:
            depth += 1
            if depth > MAX_DEPTH:
                return True
        elif character in _CLOSERS:
            depth -= 1
    return False


def _same_id(row_id: str, device_id: str) -> bool:
    """Return True if both ids are one UUID. A row id that is not a UUID is not."""
    try:
        return uuid.UUID(row_id) == uuid.UUID(device_id)
    except ValueError:
        return False


def _problems(error: ValidationError) -> str:
    """Return the place and the problem of each error, with field names only."""
    return "; ".join(
        ".".join(
            str(part)
            for part in problem["loc"]
            if isinstance(part, int) or part in _FIELD_NAMES
        )
        + f": {problem['msg']}"
        for problem in error.errors(
            include_input=False, include_url=False, include_context=False
        )
    )


def _check_id(device_id: DeviceID) -> None:
    """Refuse an id that is not a UUID, before a request.

    Raises:
        DeviceNotFoundError: If the id is not a UUID.

    """
    try:
        uuid.UUID(device_id)
    except ValueError:
        message = f"Nautobot has no device {device_id!r}; a Nautobot id is a UUID"
        raise DeviceNotFoundError(message) from None


def _skip_key(device: _Device) -> str:
    """Return the name of a skipped device, or its id if the name is not printable.

    The key holds at most `MAX_KEY_LENGTH` characters.
    """
    if device.name and device.name.isprintable():
        return device.name[:MAX_KEY_LENGTH]
    return device.id[:MAX_KEY_LENGTH]


def _record(device: _Device) -> DeviceRecord | str:
    """Return the device record, or the reason that the device is left out."""
    texts = [device.id, device.name or ""] + [tag.name for tag in device.tags]
    texts += [named.name for named in (device.role, device.location) if named]
    if not all(text.isprintable() for text in texts):
        return CONTROL_CHARACTER
    if device.platform is None:
        return NO_PLATFORM
    mapping = device.platform.network_driver_mappings.get("hier_config")
    platform_name = platform_name_of_mapping(mapping) if mapping else None
    if platform_name is None:
        return UNMAPPED_PLATFORM
    if device.primary_ip4 is None:
        return NO_ADDRESS
    try:
        address = ipaddress.ip_address(device.primary_ip4.host)
    except ValueError:
        return BAD_ADDRESS
    return DeviceRecord(
        id=DeviceID(device.id),
        name=device.name or device.id,
        platform_name=platform_name,
        mgmt_address=str(address),
        role=device.role.name if device.role else None,
        site=device.location.name if device.location else None,
        tags=frozenset(tag.name for tag in device.tags),
    )
