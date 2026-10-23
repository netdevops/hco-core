"""The Nautobot source of truth reads Nautobot 2.x over GraphQL and REST, only."""

import asyncio
import errno
import json
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from hco_core.adapters.errors import (
    AdapterConfigError,
    DeviceNotFoundError,
    InvalidInventoryError,
    SourceOfTruthUnreachableError,
)
from hco_core.adapters.sot import nautobot
from hco_core.adapters.sot.nautobot import NautobotSourceOfTruth
from hco_core.domain.device import DeviceSelector, TopologyEdge
from hco_core.domain.ids import DeviceID
from hco_core.domain.secrets import SecretPurpose, SecretRef
from hco_core.ports import SourceOfTruth

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "nautobot"
TOKEN = "0123456789abcdef-TOKEN"  # ruff: ignore[hardcoded-password-string]
LEAKS = (TOKEN, "SECRET-FROM-NAUTOBOT", "PlatformType", "user:pass")
URL = "https://user:pass@nautobot.example:8443/"
EDGE_1 = DeviceID("11111111-1111-4111-8111-111111111111")
EDGE_2 = DeviceID("22222222-2222-4222-8222-222222222222")
CORE_1 = DeviceID("33333333-3333-4333-8333-333333333333")
FAR = DeviceID("99999999-9999-4999-8999-999999999999")
GROUP_ONLY = DeviceID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
MISSING = DeviceID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")

DEEP_MAPPINGS = json.dumps(
    {
        "data": {
            "devices": [
                {"id": EDGE_1, "platform": {"network_driver_mappings": "[" * 100_000}}
            ]
        }
    }
).encode()

type Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> bytes:
    """Return the bytes of a recorded Nautobot answer."""
    return (FIXTURES / name).read_bytes()


def page_of(name: str) -> list[dict[str, object]]:
    """Return the devices of a recorded GraphQL answer."""
    answer = json.loads(fixture(name))
    devices: list[dict[str, object]] = answer["data"]["devices"]
    return devices


class FakeNautobot:
    """Answer each request from the fixtures, and record every request."""

    def __init__(self, devices: str = "devices_page.json") -> None:
        """Serve the devices of the fixture."""
        self.devices = page_of(devices)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Return the answer of the request."""
        self.requests.append(request)
        path = request.url.path
        if path == "/api/status/":
            return httpx.Response(200, json={"nautobot-version": "2.4.0"})
        if path == "/api/plugins/golden-config/golden-config/":
            wanted = request.url.params["device_id"]
            answer = json.loads(fixture("golden_config.json"))
            answer["results"] = [
                row for row in answer["results"] if row["device"]["id"] == wanted
            ]
            return httpx.Response(200, json=answer)
        query = json.loads(request.content)
        return httpx.Response(
            200, json=self.graphql(query["query"], query["variables"])
        )

    def graphql(self, query: str, variables: dict[str, object]) -> object:
        """Answer one GraphQL query by its operation name and its variables."""
        if "HcoGroup" in query:
            return json.loads(fixture("redundancy_group.json"))
        if "HcoContext" in query:
            return json.loads(fixture("config_context.json"))
        devices = self.devices
        if "id" in variables:
            wanted = variables["id"]
            assert isinstance(wanted, list)
            devices = [device for device in devices if device["id"] in wanted]
        offset = variables.get("offset", 0)
        limit = variables.get("limit", len(devices))
        assert isinstance(offset, int)
        assert isinstance(limit, int)
        return {"data": {"devices": devices[offset : offset + limit]}}


def adapter(handler: Handler, url: str = URL) -> NautobotSourceOfTruth:
    """Return an adapter that sends each request to the handler."""
    return NautobotSourceOfTruth(
        url, SecretStr(TOKEN), transport=httpx.MockTransport(handler)
    )


def answer(status: int, body: bytes) -> Handler:
    """Return a handler that gives one status and one body to each request."""
    return lambda _: httpx.Response(status, content=body)


def assert_no_leak(text: str) -> None:
    """Assert that the text holds no token, no userinfo, and no text from Nautobot."""
    for leak in LEAKS:
        assert leak not in text


def test_the_adapter_satisfies_the_port_and_hides_the_token() -> None:
    """The adapter is a `SourceOfTruth`, and repr and str show no secret."""
    sot = adapter(FakeNautobot())
    assert isinstance(sot, SourceOfTruth)
    assert sot.name == "nautobot"
    assert repr(sot) == "NautobotSourceOfTruth(url='https://nautobot.example:8443')"
    assert_no_leak(str(sot))


def test_a_graphql_page_gives_device_records() -> None:
    """One query gives the platform name, role, location, tags, and address."""
    fake = FakeNautobot()
    page = asyncio.run(adapter(fake).list_devices())
    edge_1, edge_2, core_1 = page.devices
    assert (edge_1.id, edge_1.name, edge_1.platform_name) == (
        EDGE_1,
        "edge-1",
        "CISCO_IOS",
    )
    assert (edge_1.role, edge_1.site, edge_1.mgmt_address) == (
        "edge",
        "lab",
        "10.0.0.1",
    )
    assert edge_1.tags == frozenset({"wan", "hco-redundancy-core"})
    assert edge_2.platform_name == "ARISTA_EOS"
    assert core_1.platform_name == "CISCO_XR"
    assert (page.cursor, page.total, page.skipped) == (None, None, {})
    (request,) = fake.requests
    assert (request.method, str(request.url)) == (
        "POST",
        "https://nautobot.example:8443/api/graphql/",
    )
    assert request.headers["authorization"] == f"Token {TOKEN}"
    assert "user:pass" not in str(request.url)


def test_paging_uses_the_nautobot_offset() -> None:
    """The cursor is the offset of the next page, and the last page gives None."""
    sot = adapter(FakeNautobot())
    first = asyncio.run(sot.list_devices(limit=2))
    assert [device.id for device in first.devices] == [EDGE_1, EDGE_2]
    assert first.cursor == "2"
    last = asyncio.run(sot.list_devices(cursor=first.cursor, limit=2))
    assert [device.id for device in last.devices] == [CORE_1]
    assert last.cursor is None
    for cursor, limit in (("-1", 2), ("x", 2), ("\uff11", 2), (None, 0)):
        with pytest.raises(ValueError, match=r"cursor|limit"):
            asyncio.run(sot.list_devices(cursor=cursor, limit=limit))


def test_a_large_limit_asks_nautobot_for_one_bounded_page() -> None:
    """The adapter asks for at most `MAX_PAGE_SIZE` devices, and one more."""
    fake = FakeNautobot()
    asyncio.run(adapter(fake).list_devices(limit=5000))
    variables = json.loads(fake.requests[0].content)["variables"]
    assert variables == {"limit": nautobot.MAX_PAGE_SIZE + 1, "offset": 0}


@pytest.mark.parametrize(
    ("selector", "ids"),
    [
        (DeviceSelector(sites=("lab",)), [EDGE_1, EDGE_2]),
        (DeviceSelector(platforms=("CISCO_XR",)), [CORE_1]),
        (DeviceSelector(tags_all=frozenset({"wan", "hco-redundancy-core"})), [EDGE_1]),
        (DeviceSelector(names=("edge-2",), roles=("edge",)), [EDGE_2]),
    ],
)
def test_the_selector_filters_the_page(
    selector: DeviceSelector, ids: list[DeviceID]
) -> None:
    """Selector fields AND together, as in the YAML adapter."""
    page = asyncio.run(adapter(FakeNautobot()).list_devices(selector=selector))
    assert [device.id for device in page.devices] == ids


def test_an_unmapped_platform_is_reported_by_name_and_skipped() -> None:
    """A device with no usable platform or address is never GENERIC."""
    page = asyncio.run(adapter(FakeNautobot("unmapped_platform.json")).list_devices())
    assert [device.name for device in page.devices] == ["edge-8"]
    assert page.skipped == {
        "fw-1": "the platform has no hier_config driver mapping",
        "odd-1": "the platform has no hier_config driver mapping",
        "no-address-1": "the device has no primary IPv4 address",
        "77777777-7777-4777-8777-777777777777": "the device has no platform",
        "bad-address-1": "the primary IPv4 address is not valid",
    }


def test_get_device_reads_one_device() -> None:
    """An unknown id or an id that is not a UUID is not found."""
    fake = FakeNautobot()
    sot = adapter(fake)
    assert asyncio.run(sot.get_device(EDGE_2)).name == "edge-2"
    with pytest.raises(DeviceNotFoundError, match=MISSING):
        asyncio.run(sot.get_device(MISSING))
    asked = len(fake.requests)
    with pytest.raises(DeviceNotFoundError, match="'r1'"):
        asyncio.run(sot.get_device(DeviceID("r1")))
    assert len(fake.requests) == asked


def test_get_device_refuses_an_unmapped_platform() -> None:
    """The error names the device id and the reason, never the platform."""
    sot = adapter(FakeNautobot("unmapped_platform.json"))
    device = DeviceID("44444444-4444-4444-8444-444444444444")
    with pytest.raises(InvalidInventoryError, match="hier_config driver") as error:
        asyncio.run(sot.get_device(device))
    assert "PAN-OS" not in str(error.value)


def test_intended_config_comes_from_golden_config() -> None:
    """One REST call gives the intended config, or None for no record."""
    fake = FakeNautobot()
    sot = adapter(fake)
    intended = asyncio.run(sot.intended_config(EDGE_1))
    assert intended is not None
    assert intended.body.startswith("hostname edge-1\n")
    assert intended.source == "sot"
    request = fake.requests[-1]
    assert request.method == "GET"
    assert request.url.params["device_id"] == EDGE_1
    assert asyncio.run(sot.intended_config(EDGE_2)) is None


def test_config_context_is_a_bounded_mapping() -> None:
    """The context is a mapping, and deep nesting is refused."""
    sot = adapter(FakeNautobot())
    context = asyncio.run(sot.config_context(EDGE_1))
    assert context == {"ntp_servers": ["192.0.2.10"], "snmp": {"location": "rack 4"}}
    deep: object = "leaf"
    for _ in range(nautobot.MAX_DEPTH + 1):
        deep = {"k": deep}
    body = {"data": {"devices": [{"id": EDGE_1, "config_context": deep}]}}
    deep_sot = adapter(answer(200, json.dumps(body).encode()))
    with pytest.raises(InvalidInventoryError, match="nested"):
        asyncio.run(deep_sot.config_context(EDGE_1))
    with pytest.raises(DeviceNotFoundError):
        asyncio.run(sot.config_context(MISSING))


def test_topology_gives_cable_edges_and_redundancy_groups() -> None:
    """Each cable is one edge, and a group tag gives every member of the group."""
    sot = adapter(FakeNautobot("cable_peers.json"))
    graph = asyncio.run(sot.topology([EDGE_1, EDGE_2]))
    assert graph.nodes == (EDGE_1, EDGE_2)
    assert graph.edges == (
        TopologyEdge(
            a=EDGE_1, b=EDGE_2, a_interface="Ethernet1", b_interface="Ethernet1"
        ),
        TopologyEdge(a=EDGE_1, b=FAR, a_interface="Ethernet3", b_interface="Ethernet9"),
    )
    assert graph.redundancy_groups == {"core": (EDGE_1, EDGE_2, GROUP_ONLY)}
    assert graph.shares_group(EDGE_2, GROUP_ONLY)


def context_answer(name: str | None, context: Mapping[str, object]) -> Handler:
    """Return a handler that gives one device with the name and the context."""
    device = {"id": EDGE_1, "name": name, "config_context": context}
    body = json.dumps({"data": {"devices": [device]}}).encode()
    return answer(200, body)


@pytest.mark.parametrize(
    ("name", "context", "key"),
    [
        ("edge-1", {"hco_secret_key": "lab"}, "lab"),
        ("edge-1.dc1 a", {"ntp": ["192.0.2.10"]}, "EDGE_1_DC1_A"),
        (None, {}, "11111111_1111_4111_8111_111111111111"),
    ],
)
def test_secret_ref_is_an_env_reference(
    name: str | None, context: Mapping[str, object], key: str
) -> None:
    """The key is `hco_secret_key` of the config context, else the device name."""
    sot = adapter(context_answer(name, context))
    ref = asyncio.run(sot.secret_ref(EDGE_1, SecretPurpose.DEVICE_ENABLE))
    assert ref == SecretRef(
        provider="env", key=key, purpose=SecretPurpose.DEVICE_ENABLE
    )


def test_secret_ref_never_carries_a_value() -> None:
    """The reference has only the provider, the key, and the purpose."""
    context = {"hco_secret_key": "lab", "password": "SECRET-FROM-NAUTOBOT"}
    sot = adapter(context_answer("edge-1", context))
    ref = asyncio.run(sot.secret_ref(EDGE_1, SecretPurpose.DEVICE_LOGIN))
    dumped = ref.model_dump_json()
    assert set(ref.model_dump()) == {"provider", "key", "purpose"}
    assert_no_leak(dumped)
    assert_no_leak(repr(ref))


@pytest.mark.parametrize(
    "value",
    [7, "", "   ", ["lab"], "x" * 129],
    ids=["int", "empty", "blank", "list", "long"],
)
def test_a_bad_hco_secret_key_is_refused_without_its_value(value: object) -> None:
    """The error names the device and the key, never the value."""
    sot = adapter(context_answer("edge-1", {"hco_secret_key": value}))
    with pytest.raises(InvalidInventoryError, match="'hco_secret_key'") as error:
        asyncio.run(sot.secret_ref(EDGE_1, SecretPurpose.DEVICE_LOGIN))
    assert EDGE_1 in str(error.value)
    assert "x" * 129 not in str(error.value)


@pytest.mark.parametrize(
    ("status", "body", "setting", "code"),
    [
        (401, fixture("unauthorized.json"), "HCO_NAUTOBOT_TOKEN", errno.EACCES),
        (403, fixture("unauthorized.json"), "HCO_NAUTOBOT_TOKEN", errno.EACCES),
        (404, b"", "HTTP 404", errno.ENOENT),
        (500, fixture("server_error.html"), "HTTP 500", errno.EPROTO),
        (302, b"", "HTTP 302", errno.EPROTO),
    ],
)
def test_a_refusal_is_unreachable_and_names_the_setting(
    status: int, body: bytes, setting: str, code: int
) -> None:
    """The error is an `OSError` with an errno, so device sync answers 503."""
    with pytest.raises(SourceOfTruthUnreachableError, match=setting) as error:
        asyncio.run(adapter(answer(status, body)).list_devices())
    assert isinstance(error.value, OSError)
    assert error.value.errno == code
    assert error.value.__cause__ is None
    assert "nautobot.example:8443" in str(error.value)
    assert_no_leak(str(error.value))


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (httpx.ConnectError, errno.ECONNREFUSED),
        (httpx.ReadTimeout, errno.ETIMEDOUT),
        (httpx.RemoteProtocolError, errno.EIO),
    ],
)
def test_a_transport_error_has_an_errno(
    failure: type[httpx.TransportError], code: int
) -> None:
    """The errno gives the log a fixed reason for each kind of failure."""

    def fail(request: httpx.Request) -> httpx.Response:
        message = "no answer"
        raise failure(message, request=request)

    with pytest.raises(SourceOfTruthUnreachableError) as error:
        asyncio.run(adapter(fail).list_devices())
    assert error.value.errno == code


def test_a_golden_config_404_names_the_app() -> None:
    """A 404 on the Golden Config route says that the app does not answer."""

    def no_app(request: httpx.Request) -> httpx.Response:
        if request.url.path == nautobot.GOLDEN_CONFIG_PATH:
            return httpx.Response(404)
        return FakeNautobot()(request)

    with pytest.raises(SourceOfTruthUnreachableError, match="Golden Config app"):
        asyncio.run(adapter(no_app).intended_config(EDGE_1))


def test_a_transport_error_hides_the_request() -> None:
    """The error names the error type, never the request or its headers."""

    def refuse(request: httpx.Request) -> httpx.Response:
        message = f"refused {request.headers['authorization']} {request.url}"
        raise httpx.ConnectError(message, request=request)

    with pytest.raises(SourceOfTruthUnreachableError, match="ConnectError") as error:
        asyncio.run(adapter(refuse).get_device(EDGE_1))
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    assert_no_leak(str(error.value))


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (fixture("malformed.json"), "not valid JSON"),
        (b"\xff\xfe{}", "not UTF-8"),
        (b"[1, 2]", "expected shape"),
        (b'{"data": {"devices": [{"id": 7}]}}', "expected shape"),
        (b"[" * 100_000 + b"]" * 100_000, "nested"),
        (DEEP_MAPPINGS, "expected shape"),
        (fixture("graphql_errors.json"), "refused the query"),
    ],
    ids=["malformed", "latin", "list", "id", "deep", "deep-mappings", "errors"],
)
def test_a_bad_answer_is_invalid_and_quotes_nothing(body: bytes, problem: str) -> None:
    """The error says what is wrong, never what Nautobot sent."""
    with pytest.raises(InvalidInventoryError, match=problem) as error:
        asyncio.run(adapter(answer(200, body)).list_devices())
    assert_no_leak(str(error.value))


def test_a_huge_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter stops reading at `MAX_RESPONSE_BYTES`, with or without a length."""
    monkeypatch.setattr(nautobot, "MAX_RESPONSE_BYTES", 1024)
    huge = b'{"data": {"devices": []}, "pad": "' + b"x" * 2048 + b'"}'

    async def chunks() -> AsyncIterator[bytes]:
        yield huge[:600]
        await asyncio.sleep(0)
        yield huge[600:]

    def chunked(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    for handler in (answer(200, huge), chunked):
        with pytest.raises(InvalidInventoryError, match="larger than 1024 bytes"):
            asyncio.run(adapter(handler).list_devices())


def test_health_reports_reachability_without_userinfo() -> None:
    """The detail names the host, never the user name, the password, or the token."""
    healthy = asyncio.run(adapter(FakeNautobot()).health())
    assert healthy.healthy
    assert healthy.detail == "Nautobot at https://nautobot.example:8443 answers"
    refused = asyncio.run(adapter(answer(401, fixture("unauthorized.json"))).health())
    assert not refused.healthy
    assert "HCO_NAUTOBOT_TOKEN" in refused.detail
    assert_no_leak(refused.detail)


def test_the_adapter_never_writes_to_nautobot() -> None:
    """Each call is a GET, or a POST of a GraphQL query, never a mutation."""
    fake = FakeNautobot()
    sot = adapter(fake)

    async def call_everything() -> None:
        await sot.health()
        await sot.list_devices()
        await sot.get_device(EDGE_1)
        await sot.intended_config(EDGE_1)
        await sot.config_context(EDGE_1)
        await sot.topology([EDGE_1])
        await sot.secret_ref(EDGE_1, SecretPurpose.DEVICE_LOGIN)

    asyncio.run(call_everything())
    assert {request.method for request in fake.requests} == {"GET", "POST"}
    for request in fake.requests:
        if request.method == "POST":
            assert request.url.path == "/api/graphql/"
            assert json.loads(request.content)["query"].lstrip().startswith("query ")


@pytest.mark.parametrize(
    ("url", "token", "problem"),
    [
        ("ftp://nautobot.example", TOKEN, "http or https"),
        ("nautobot.example", TOKEN, "http or https"),
        ("https://[::1", TOKEN, "http or https"),
        ("https://nautobot.example", " ", "HCO_NAUTOBOT_TOKEN"),
        ("https://nautobot.example", "t\u00e9" + TOKEN, "HCO_NAUTOBOT_TOKEN"),
        ("https://nautobot.example", TOKEN + "\n", "HCO_NAUTOBOT_TOKEN"),
    ],
    ids=["ftp", "no-scheme", "bad-host", "blank", "non-ascii", "newline"],
)
def test_a_bad_url_or_token_is_refused(url: str, token: str, problem: str) -> None:
    """The error names the problem, never the token."""
    with pytest.raises(AdapterConfigError, match=problem) as error:
        NautobotSourceOfTruth(url, SecretStr(token))
    assert_no_leak(str(error.value))


@pytest.mark.parametrize(
    "rows",
    [
        [{"device": {"id": EDGE_2}, "intended_config": "hostname OTHER"}],
        [{"intended_config": "hostname NO-DEVICE"}],
        [
            {"device": {"id": EDGE_1}, "intended_config": "hostname ONE"},
            {"device": {"id": EDGE_1}, "intended_config": "hostname TWO"},
        ],
    ],
    ids=["other-device", "no-device", "two-rows"],
)
def test_intended_config_refuses_a_row_that_is_not_the_device(
    rows: list[dict[str, object]],
) -> None:
    """Only one row of the device itself gives an intended config."""
    body = json.dumps({"count": len(rows), "results": rows}).encode()
    with pytest.raises(InvalidInventoryError, match="Golden Config") as error:
        asyncio.run(adapter(answer(200, body)).intended_config(EDGE_1))
    assert "hostname" not in str(error.value)


def test_a_device_with_a_control_character_is_skipped() -> None:
    """A NUL or an escape in a text field skips the device, with a safe key."""
    devices = page_of("devices_page.json")
    devices[0]["name"] = "edge\x00one"
    devices[1]["role"] = {"name": "esc\x1b[31mred"}
    devices[2]["tags"] = [{"name": "ok"}, {"name": "bad\ttag"}]
    body = json.dumps({"data": {"devices": devices}}).encode()
    page = asyncio.run(adapter(answer(200, body)).list_devices())
    assert page.devices == ()
    assert page.skipped == {
        EDGE_1: nautobot.CONTROL_CHARACTER,
        "edge-2": nautobot.CONTROL_CHARACTER,
        "core-1": nautobot.CONTROL_CHARACTER,
    }


def test_turning_off_tls_verification_logs_a_warning() -> None:
    """One warning names the URL without userinfo when `verify_ssl` is False."""
    with capture_logs() as lines:
        NautobotSourceOfTruth(URL, SecretStr(TOKEN), verify_ssl=False)
        NautobotSourceOfTruth(URL, SecretStr(TOKEN))
    assert lines == [
        {
            "event": "nautobot_tls_verification_disabled",
            "log_level": "warning",
            "url": "https://nautobot.example:8443",
        }
    ]


def test_an_unclosed_deep_answer_is_refused() -> None:
    """Brackets that never close are refused; the message depends on `json`."""
    sot = adapter(answer(200, b"[" * 100_000))
    with pytest.raises(InvalidInventoryError, match=r"nested|not valid JSON"):
        asyncio.run(sot.list_devices())
