"""The YAML source of truth reads devices, intent, and topology from one file."""

import asyncio
from pathlib import Path

import pytest

from hco_core.adapters.errors import DeviceNotFoundError, InvalidInventoryError
from hco_core.adapters.sot import yaml_file
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.domain.config import ConfigFormat
from hco_core.domain.device import DeviceSelector
from hco_core.domain.ids import DeviceID
from hco_core.domain.secrets import SecretPurpose, SecretRef
from hco_core.ports import SourceOfTruth

INVENTORY = Path(__file__).resolve().parent.parent / "fixtures" / "inventory.yaml"
R1, S1, X1 = DeviceID("r1"), DeviceID("s1"), DeviceID("x1")
DEVICE = "  - {id: r1, name: edge-1, platform_name: CISCO_IOS, mgmt_address: 10.0.0.1"


def write(tmp_path: Path, text: str) -> YamlSourceOfTruth:
    """Return a source of truth over an inventory with the text."""
    path = tmp_path / "inventory.yaml"
    path.write_text(text, encoding="utf8")
    return YamlSourceOfTruth(path)


def test_the_adapter_satisfies_the_port() -> None:
    """The adapter is a `SourceOfTruth` named yaml."""
    sot = YamlSourceOfTruth(INVENTORY)
    assert isinstance(sot, SourceOfTruth)
    assert sot.name == "yaml"


def test_list_get_and_metadata() -> None:
    """Devices keep their file order, and metadata scalars become strings."""
    sot = YamlSourceOfTruth(INVENTORY)
    page = asyncio.run(sot.list_devices())
    assert [device.id for device in page.devices] == [R1, S1, X1]
    assert (page.cursor, page.total) == (None, 3)
    device = asyncio.run(sot.get_device(R1))
    assert device.metadata == {"rack": "4", "owner": "neteng"}
    assert device.tags == frozenset({"edge", "wan"})
    assert device.criticality == "high"


@pytest.mark.parametrize(
    ("selector", "ids"),
    [
        (DeviceSelector(sites=("lab",)), [R1, S1]),
        (DeviceSelector(platforms=("CISCO_XR",)), [X1]),
        (DeviceSelector(tags_any=frozenset({"wan", "leaf"})), [R1, S1]),
        (DeviceSelector(tags_all=frozenset({"edge", "wan"})), [R1]),
        (DeviceSelector(names=("leaf-1",), roles=("core",)), []),
    ],
)
def test_the_selector_filters(selector: DeviceSelector, ids: list[DeviceID]) -> None:
    """Selector fields AND together."""
    page = asyncio.run(YamlSourceOfTruth(INVENTORY).list_devices(selector=selector))
    assert [device.id for device in page.devices] == ids


def test_paging_uses_the_cursor() -> None:
    """Each page gives the cursor of the next, and the last page gives None."""
    sot = YamlSourceOfTruth(INVENTORY)
    first = asyncio.run(sot.list_devices(limit=2))
    assert [device.id for device in first.devices] == [R1, S1]
    last = asyncio.run(sot.list_devices(cursor=first.cursor, limit=2))
    assert [device.id for device in last.devices] == [X1]
    assert last.cursor is None
    for cursor, limit in (("-1", 2), ("x", 2), (None, 0)):
        with pytest.raises(ValueError, match=r"cursor|limit"):
            asyncio.run(sot.list_devices(cursor=cursor, limit=limit))


def test_intended_config_context_and_secret_ref() -> None:
    """Intent comes from a file beside the inventory, and the secret is a reference."""
    sot = YamlSourceOfTruth(INVENTORY)
    intended = asyncio.run(sot.intended_config(R1))
    assert intended is not None
    assert intended.body.startswith("interface GigabitEthernet1/0/1\n")
    assert (intended.source, intended.format) == ("sot", ConfigFormat.CLI)
    assert asyncio.run(sot.intended_config(S1)) is None
    assert asyncio.run(sot.config_context(R1)) == {"ntp_servers": ["192.0.2.10"]}
    assert asyncio.run(sot.config_context(S1)) == {}
    purpose = SecretPurpose.DEVICE_LOGIN
    assert asyncio.run(sot.secret_ref(R1, purpose)) == SecretRef(
        provider="env", key="lab"
    )
    assert asyncio.run(sot.secret_ref(S1, purpose)).key == "s1"


def test_topology_keeps_the_edges_and_groups_of_the_devices() -> None:
    """The graph holds the edges and groups that touch a requested device."""
    sot = YamlSourceOfTruth(INVENTORY)
    graph = asyncio.run(sot.topology([R1]))
    assert graph.nodes == (R1,)
    assert graph.is_single_homed(R1)
    assert dict(graph.redundancy_groups) == {}
    graph = asyncio.run(sot.topology([S1]))
    assert set(graph.neighbors(S1)) == {R1, X1}
    assert graph.shares_group(S1, X1)


def test_an_unknown_device_is_not_found() -> None:
    """Each lookup by id refuses an id the file does not hold."""
    sot = YamlSourceOfTruth(INVENTORY)
    unknown = DeviceID("nope")
    for call in (
        sot.get_device(unknown),
        sot.intended_config(unknown),
        sot.config_context(unknown),
        sot.secret_ref(unknown, SecretPurpose.DEVICE_LOGIN),
    ):
        with pytest.raises(DeviceNotFoundError, match="nope"):
            asyncio.run(call)


@pytest.mark.parametrize("text", ["", "devices: []\n"])
def test_an_empty_inventory_lists_zero_devices(tmp_path: Path, text: str) -> None:
    """An empty file is a valid inventory with no devices."""
    page = asyncio.run(write(tmp_path, text).list_devices())
    assert (page.devices, page.total) == ((), 0)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"devices:\n{DEVICE.replace('CISCO_IOS', 'NOPE_OS')}}}\n", "'r1'.*NOPE_OS"),
        (f"devices:\n{DEVICE}, metadata: {{rack: {{row: 1}}}}}}\n", "'r1'.*rack"),
        (f"devices:\n{DEVICE}}}\n{DEVICE}}}\n", "r1.*more than once"),
        (
            f"devices:\n{DEVICE}, intended_config: ../x.txt}}\n",
            "'r1'.*intended_config",
        ),
        (
            f"devices:\n{DEVICE}, intended_config: missing.txt}}\n",
            "'r1'.*missing.txt",
        ),
        (f"devices:\n{DEVICE}, criticality: extreme}}\n", "'r1'"),
        (
            f"devices:\n{DEVICE.replace('CISCO_IOS', 'cisco_ios')}}}\n",
            "'r1'.*use CISCO_IOS",
        ),
        (f"devices:\n{DEVICE}, x: &a [1, 1]}}\n  - {{y: *a}}\n", "aliases"),
        ("devices: " + "[" * 5000 + "]" * 5000 + "\n", "nested"),
        ("devices: {r1: 1}\n", "devices"),
        ("devices: []\nx: 2001-02-30\n", "date or time value is not valid; put quotes"),
        (
            f"devices:\n{DEVICE}, config_context: {{d: 2026-10-16}}}}\n",
            r"^device 'r1', config_context key 'd': .*put quotes around a date",
        ),
        ("- r1\n", "mapping"),
        ("devices: [\n", r"YAML.*line 2, column 1"),
        ("devices: \x01\n", "ReaderError"),
    ],
)
def test_an_invalid_inventory_is_refused(
    tmp_path: Path, text: str, message: str
) -> None:
    """The error names the device and the key that is wrong."""
    sot = write(tmp_path, text)
    with pytest.raises(InvalidInventoryError, match=message):
        asyncio.run(sot.intended_config(R1))


def test_health_reports_if_the_file_is_readable(tmp_path: Path) -> None:
    """A readable inventory is healthy, and a missing one is not."""
    healthy = asyncio.run(YamlSourceOfTruth(INVENTORY).health())
    assert healthy.healthy
    assert healthy.checked_at is not None
    missing = asyncio.run(YamlSourceOfTruth(tmp_path / "none.yaml").health())
    assert not missing.healthy
    assert "none.yaml" in missing.detail


SECRET = "hunter2-SECRET"  # ruff: ignore[hardcoded-password-string]


@pytest.mark.parametrize(
    "text",
    [
        f"devices:\n{DEVICE}, password: {SECRET}}}\n",
        f"devices:\n{DEVICE}, in_maintenance: {SECRET}}}\n",
        f"devices: {SECRET}\n",
        f'devices:\n  - password: "{SECRET}}}\n',
        f"devices: !{SECRET} x\n",
    ],
)
def test_an_error_never_repeats_an_inventory_value(tmp_path: Path, text: str) -> None:
    """A value in the file is not in the error text or in the health detail."""
    sot = write(tmp_path, text)
    with pytest.raises(InvalidInventoryError) as error:
        asyncio.run(sot.list_devices())
    assert SECRET not in str(error.value)
    health = asyncio.run(sot.health())
    assert not health.healthy
    assert SECRET not in health.detail


def test_a_file_that_is_not_utf8_is_not_healthy(tmp_path: Path) -> None:
    """Bytes that are not UTF-8 make the inventory invalid."""
    path = tmp_path / "inventory.yaml"
    path.write_bytes(b"devices: [\xff\xfe]\n")
    sot = YamlSourceOfTruth(path)
    assert not asyncio.run(sot.health()).healthy
    with pytest.raises(InvalidInventoryError, match="UTF-8"):
        asyncio.run(sot.list_devices())


def test_an_intended_config_that_is_not_utf8_is_invalid(tmp_path: Path) -> None:
    """The intended config file must be UTF-8 too."""
    (tmp_path / "r1.txt").write_bytes(b"\xff\xfe")
    sot = write(tmp_path, f"devices:\n{DEVICE}, intended_config: r1.txt}}\n")
    with pytest.raises(InvalidInventoryError, match=r"'r1'.*UTF-8"):
        asyncio.run(sot.intended_config(R1))


def test_a_file_over_the_size_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter refuses a file larger than the limit before it reads it."""
    monkeypatch.setattr(yaml_file, "MAX_INVENTORY_BYTES", 10)
    with pytest.raises(InvalidInventoryError, match="10 bytes"):
        asyncio.run(write(tmp_path, "devices: []\n").list_devices())


def test_a_date_that_does_not_exist_is_not_healthy(tmp_path: Path) -> None:
    """A date such as 2001-02-30 makes the inventory invalid, not a crash."""
    sot = write(tmp_path, "devices: []\nx: 2001-02-30\n")
    assert not asyncio.run(sot.health()).healthy


def test_a_metadata_date_becomes_a_string(tmp_path: Path) -> None:
    """An unquoted date in metadata is a scalar, so it becomes its text."""
    sot = write(tmp_path, f"devices:\n{DEVICE}, metadata: {{since: 2026-10-16}}}}\n")
    assert asyncio.run(sot.get_device(R1)).metadata == {"since": "2026-10-16"}
