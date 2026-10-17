"""Config-derived facts describe what a remediation touches."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from hier_config.models import MatchRule, ReferenceLocation, UnusedObjectRule
from hier_config.platforms.cisco_ios.driver import HConfigDriverCiscoIOS
from hier_config.registry import register_driver, unregister_driver

from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.domain.remediation import ConfigDerived
from hco_core.engine.errors import InvalidConfigError, UnknownPlatformError
from hco_core.engine.remediation import build_bundle, parse
from hco_core.engine.views import config_derived

CONFIGS = Path(__file__).resolve().parent.parent / "fixtures" / "configs"
IOS = "CISCO_IOS"
UNUSED_ACL_PLATFORM = "HCO_TEST_UNUSED_ACL"
SWITCH = """interface GigabitEthernet0/0
 ip address 10.0.0.1 255.255.255.0
interface GigabitEthernet0/1
 description access
 switchport mode access
 switchport access vlan 10
 authentication port-control auto
 channel-group 5 mode active
interface GigabitEthernet0/2
 description spare
vlan 10
vlan 20
 name users
"""


def derive(
    platform_name: str, running: str, intended: str, mgmt_address: str = "10.0.0.1"
) -> ConfigDerived:
    """Return the facts of the change from one config text to another."""
    running_dump = parse(platform_name, running)
    bundle = build_bundle(platform_name, running_dump, parse(platform_name, intended))
    device = DeviceRecord(
        id=DeviceID("d1"),
        name="r1",
        platform_name=platform_name,
        mgmt_address=mgmt_address,
        metadata={},
    )
    return config_derived(bundle, running_dump, device)


def fixture_facts(platform_name: str) -> ConfigDerived:
    """Return the facts of the fixture change of one platform."""
    folder = CONFIGS / platform_name.lower()
    texts = (folder / f"{name}.txt" for name in ("running", "intended"))
    running, intended = (text.read_text(encoding="utf8") for text in texts)
    return derive(platform_name, running, intended)


@pytest.mark.parametrize(
    ("platform_name", "interface", "commands", "negations", "sections"),
    [
        (IOS, "GigabitEthernet1/0/1", (4, 1), (), ("ip routing",)),
        ("CISCO_NXOS", "Ethernet1/1", (5, 2), ("no feature lacp",), ("feature lldp",)),
        ("CISCO_XR", "GigabitEthernet0/0/0/0", (4, 1), (), ("lldp",)),
        ("ARISTA_EOS", "Ethernet1", (5, 2), (), ("ip routing",)),
    ],
)
def test_fixture_facts_on_each_platform(
    platform_name: str,
    interface: str,
    commands: tuple[int, int],
    negations: tuple[str, ...],
    sections: tuple[str, ...],
) -> None:
    """Each v1 platform reports the touched interface and the line counts."""
    facts = fixture_facts(platform_name)
    assert facts.interfaces_touched == (interface,)
    assert (facts.command_count, facts.negation_count) == commands
    assert facts.section_negations == negations
    assert facts.top_level_sections == tuple(
        sorted((f"interface {interface}", *sections, *negations))
    )
    assert not facts.touches_mgmt_path
    assert facts.unused_objects_created == ()


def test_interface_facts_include_bundles_vlans_and_nac() -> None:
    """A touched switchport reports its bundle, its VLAN, and its NAC state."""
    facts = derive(IOS, SWITCH, SWITCH.replace(" access\n", " users\n"))
    assert facts.interfaces_touched == ("GigabitEthernet0/1",)
    assert facts.bundles_touched == ("Port-channel5",)
    assert facts.vlans_touched == (10,)
    assert facts.nac_interfaces_touched == ("GigabitEthernet0/1",)


def test_vlan_definition_changes_are_vlans_touched() -> None:
    """A VLAN added, removed, or renamed is touched, also while a port uses it."""
    intended = SWITCH.replace("\nvlan 10\n", "\n").replace(" users\n", " guests\n")
    facts = derive(IOS, SWITCH, f"{intended}vlan 30\n")
    assert facts.vlans_touched == (10, 20, 30)
    assert facts.interfaces_touched == ()
    assert facts.section_negations == ("no vlan 10",)


def test_interface_removal_is_an_interface_touched() -> None:
    """A removed interface is touched, with the native VLAN 1 the view gives it."""
    spare = "interface GigabitEthernet0/2\n description spare\n"
    facts = derive(IOS, SWITCH, SWITCH.replace(spare, ""))
    assert facts.interfaces_touched == ("GigabitEthernet0/2",)
    assert facts.section_negations == ("no interface GigabitEthernet0/2",)
    assert facts.vlans_touched == (1,)


def test_remediation_tags_are_reported() -> None:
    """The tags that tag rules apply are reported, sorted."""
    rule = {"match_rules": [{"startswith": "interface"}], "apply_tags": ["b", "a"]}
    running_dump = parse(IOS, SWITCH)
    intended_dump = parse(IOS, SWITCH.replace(" spare\n", " x\n"))
    bundle = build_bundle(IOS, running_dump, intended_dump, tag_rules=(rule,))
    device = DeviceRecord(
        id=DeviceID("d1"),
        name="r1",
        platform_name=IOS,
        mgmt_address="10.0.0.1",
        metadata={},
    )
    assert config_derived(bundle, running_dump, device).remediation_tags == ("a", "b")


@pytest.mark.parametrize(
    ("mgmt_address", "touches"),
    [
        ("10.0.0.1", True),
        ("10.0.0.1/24", True),
        ("10.0.0.9", False),
        ("r1.lab", False),
        ("2001:db8::1", False),
    ],
)
def test_changing_the_management_interface_touches_the_mgmt_path(
    mgmt_address: str, *, touches: bool
) -> None:
    """The interface that holds the management address is found in the running view."""
    intended = SWITCH.replace("10.0.0.1 ", "10.0.0.2 ")
    assert derive(IOS, SWITCH, intended, mgmt_address).touches_mgmt_path is touches


def test_another_interface_does_not_touch_the_mgmt_path() -> None:
    """A change on a different interface leaves the management path alone."""
    assert not derive(IOS, SWITCH, SWITCH.replace(" spare\n", " x\n")).touches_mgmt_path


DEFAULT_ROUTES = [
    (IOS, "ip route 0.0.0.0 0.0.0.0 192.0.2.1\n"),
    ("CISCO_NXOS", "ip route 0.0.0.0/0 192.0.2.1\n"),
    ("CISCO_NXOS", "vrf context management\n  ip route 0.0.0.0/0 192.0.2.1\n"),
    ("ARISTA_EOS", "ip route 0.0.0.0/0 192.0.2.1\n"),
    (
        "CISCO_XR",
        "router static\n address-family ipv4 unicast\n  0.0.0.0/0 192.0.2.1\n",
    ),
]


@pytest.mark.parametrize(("platform_name", "route"), DEFAULT_ROUTES)
def test_adding_or_removing_a_default_route_touches_the_mgmt_path(
    platform_name: str, route: str
) -> None:
    """A default route added or removed can cut the path HCO uses."""
    assert derive(platform_name, "", route).touches_mgmt_path
    assert derive(platform_name, route, "").touches_mgmt_path


@pytest.mark.parametrize(("platform_name", "route"), DEFAULT_ROUTES)
def test_another_static_route_does_not_touch_the_mgmt_path(
    platform_name: str, route: str
) -> None:
    """Only the default route counts, not every static route."""
    other = route.replace("0.0.0.0 0.0.0.0", "10.9.0.0 255.255.0.0")
    other = other.replace("0.0.0.0/0", "10.9.0.0/16")
    assert not derive(platform_name, "", other).touches_mgmt_path


class UnusedAclDriver(HConfigDriverCiscoIOS):
    """An IOS driver that reports an extended ACL no interface uses."""

    def __init__(self) -> None:
        """Add one unused-object rule to the IOS rules."""
        super().__init__()
        self.rules.unused_objects.append(
            UnusedObjectRule(
                match_rules=(MatchRule(startswith="ip access-list extended "),),
                name_re=r"^ip access-list extended (?P<name>\S+)",
                reference_locations=(
                    ReferenceLocation(
                        match_rules=(MatchRule(startswith="interface "),),
                        reference_re=r"ip access-group {name} ",
                    ),
                ),
            )
        )


@pytest.fixture
def unused_acl_platform() -> Iterator[str]:
    """Register the test-only driver under a string name, then remove it.

    Yields:
        The platform name of the test-only driver.

    """
    register_driver(UNUSED_ACL_PLATFORM, UnusedAclDriver)
    yield UNUSED_ACL_PLATFORM
    unregister_driver(UNUSED_ACL_PLATFORM)


def test_unused_objects_created_is_after_minus_before(unused_acl_platform: str) -> None:
    """An ACL left defined but no longer used is a created unused object."""
    acls = "ip access-list extended A\n permit ip any any\nip access-list extended B\n"
    running = f"{acls} permit ip any any\ninterface Gi1\n ip access-group B in\n"
    intended = f"{acls} permit ip any any\ninterface Gi1\n ip access-group A in\n"
    facts = derive(unused_acl_platform, running, intended)
    assert facts.unused_objects_before == ("ip access-list extended A",)
    assert facts.unused_objects_after == ("ip access-list extended B",)
    assert facts.unused_objects_created == ("ip access-list extended B",)
    assert not facts.touches_mgmt_path


def test_bad_input_raises_engine_errors() -> None:
    """A malformed running dump or an unknown platform gives an engine error."""
    running_dump = parse(IOS, SWITCH)
    bundle = build_bundle(IOS, running_dump, running_dump)
    device = DeviceRecord(
        id=DeviceID("d1"), name="r1", platform_name=IOS, mgmt_address="", metadata={}
    )
    with pytest.raises(InvalidConfigError):
        config_derived(bundle, {"lines": "x"}, device)
    unknown = bundle.model_copy(update={"platform_name": "HCO_TEST_NO_DRIVER"})
    with pytest.raises(UnknownPlatformError):
        config_derived(unknown, running_dump, device)


GATEWAY = "interface Vlan1\n ip address 10.0.0.1 255.255.255.0\n"


@pytest.mark.parametrize(
    ("running", "intended"),
    [
        ("", "ip default-gateway 10.0.0.254\n"),
        ("ip default-gateway 10.0.0.254\n", ""),
        ("ip default-gateway 10.0.0.254\n", "ip default-gateway 10.0.0.253\n"),
    ],
)
def test_an_ios_default_gateway_change_touches_the_mgmt_path(
    running: str, intended: str
) -> None:
    """The view's default gateway is part of the path HCO uses."""
    facts = derive(IOS, GATEWAY + running, GATEWAY + intended, "10.0.0.9")
    assert facts.touches_mgmt_path


def test_an_unchanged_default_gateway_does_not_touch_the_mgmt_path() -> None:
    """A change elsewhere leaves the default gateway alone."""
    running = f"{GATEWAY}ip default-gateway 10.0.0.254\n"
    assert not derive(IOS, running, f"{running}ip routing\n").touches_mgmt_path


@pytest.mark.parametrize(
    ("platform_name", "route"),
    [
        (IOS, "ip route vrf MGMT 0.0.0.0 0.0.0.0 192.0.2.1\n"),
        ("ARISTA_EOS", "ip route vrf MGMT 0.0.0.0/0 192.0.2.1\n"),
        (UNUSED_ACL_PLATFORM, "ip route 0.0.0.0 0.0.0.0 192.0.2.1\n"),
    ],
)
@pytest.mark.usefixtures("unused_acl_platform")
def test_known_limits_of_the_default_route_check(
    platform_name: str, route: str
) -> None:
    """A VRF default route, or a platform outside the table, is not found."""
    assert not derive(platform_name, "", route).touches_mgmt_path


def test_a_deep_default_route_is_found() -> None:
    """A default route at the end of a deep lineage still matches."""
    chain = "".join(f"{' ' * depth}level {depth}\n" for depth in range(300))
    route = f"{chain}{' ' * 299} router static\n{' ' * 300} address-family ipv4 unicast"
    deep = f"{route}\n{' ' * 301} 0.0.0.0/0 192.0.2.1\n"
    assert derive("CISCO_XR", chain, deep).touches_mgmt_path
    assert not derive(
        "CISCO_XR", chain, deep.replace("0.0.0.0/0", "10.9.0.0/16")
    ).touches_mgmt_path
