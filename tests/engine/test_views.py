"""Config-derived facts describe what a remediation touches."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from hier_config.models import MatchRule, ReferenceLocation, UnusedObjectRule
from hier_config.platforms.cisco_ios.driver import HConfigDriverCiscoIOS
from hier_config.registry import register_driver, unregister_driver

from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.domain.remediation import ConfigDerived, DefaultRouteFact
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


def routes(facts: ConfigDerived) -> list[tuple[str | None, bool]]:
    """Return the VRF and the negation of each default route touched."""
    return [(route.vrf, route.negated) for route in facts.default_routes_touched]


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


@pytest.mark.usefixtures("unused_acl_platform")
def test_a_platform_outside_the_table_finds_no_default_route() -> None:
    """A platform outside the table has no default route prefixes."""
    route = "ip route 0.0.0.0 0.0.0.0 192.0.2.1\n"
    facts = derive(UNUSED_ACL_PLATFORM, "", route)
    assert facts.default_routes_touched == ()
    assert not facts.touches_mgmt_path


MGMT = {
    IOS: (
        "Gi0/0",
        "interface Gi0/0\n vrf forwarding {vrf}\n ip address 10.0.0.1 255.255.255.0\n",
    ),
    "CISCO_NXOS": (
        "mgmt0",
        "interface mgmt0\n  vrf member {vrf}\n  ip address 10.0.0.1/24\n",
    ),
    "ARISTA_EOS": (
        "Management1",
        "interface Management1\n   vrf {vrf}\n   ip address 10.0.0.1/24\n",
    ),
    "CISCO_XR": (
        "MgmtEth0",
        "interface MgmtEth0\n vrf {vrf}\n ipv4 address 10.0.0.1 255.255.255.0\n",
    ),
}
ROUTES = {
    IOS: (
        "ip route 0.0.0.0 0.0.0.0 10.0.0.254\n",
        "ip route vrf {vrf} 0.0.0.0 0.0.0.0 10.0.0.254\n",
    ),
    "CISCO_NXOS": (
        "ip route 0.0.0.0/0 10.0.0.254\n",
        "vrf context {vrf}\n  ip route 0.0.0.0/0 10.0.0.254\n",
    ),
    "ARISTA_EOS": (
        "ip route 0.0.0.0/0 10.0.0.254\n",
        "ip route vrf {vrf} 0.0.0.0/0 10.0.0.254\n",
    ),
    "CISCO_XR": (
        "router static\n address-family ipv4 unicast\n  0.0.0.0/0 10.0.0.254\n",
        "router static\n vrf {vrf}\n  address-family ipv4 unicast\n   0.0.0.0/0 10.0.0.254\n",
    ),
}


@pytest.mark.parametrize("platform_name", sorted(MGMT))
@pytest.mark.parametrize(
    ("route_vrf", "touches"), [("MGMT", True), ("CUST", False), (None, False)]
)
def test_a_default_route_touches_the_mgmt_path_only_in_the_mgmt_vrf(
    platform_name: str, route_vrf: str | None, *, touches: bool
) -> None:
    """A default route in another VRF, or in the global table, is not the path."""
    interface, template = MGMT[platform_name]
    mgmt = template.format(vrf="MGMT")
    global_route, vrf_route = ROUTES[platform_name]
    route = vrf_route.format(vrf=route_vrf) if route_vrf else global_route
    for running, intended, negated in (
        (mgmt, mgmt + route, False),
        (mgmt + route, mgmt, True),
    ):
        facts = derive(platform_name, running, intended)
        assert (facts.mgmt_interface, facts.mgmt_vrf) == (interface, "MGMT")
        assert routes(facts) == [(route_vrf, negated)]
        assert facts.touches_mgmt_path is touches


@pytest.mark.parametrize("route_vrf", ["CUST", None])
def test_without_a_mgmt_vrf_any_default_route_touches_the_path(
    route_vrf: str | None,
) -> None:
    """A management interface in the global table has no VRF, so every route counts."""
    global_route, vrf_route = ROUTES[IOS]
    route = vrf_route.format(vrf=route_vrf) if route_vrf else global_route
    facts = derive(IOS, GATEWAY, GATEWAY + route)
    assert (facts.mgmt_interface, facts.mgmt_vrf) == ("Vlan1", None)
    assert facts.touches_mgmt_path


@pytest.mark.parametrize("platform_name", sorted(ROUTES))
def test_any_default_route_touches_the_path_without_a_mgmt_interface(
    platform_name: str,
) -> None:
    """An unknown management VRF counts every default route."""
    route = ROUTES[platform_name][1].format(vrf="CUST")
    facts = derive(platform_name, "", route)
    assert (facts.mgmt_interface, facts.mgmt_vrf) == (None, None)
    assert routes(facts) == [("CUST", False)]
    assert facts.touches_mgmt_path


def test_a_replaced_default_route_is_two_facts() -> None:
    """A changed next hop removes one default route and adds another."""
    mgmt = MGMT[IOS][1].format(vrf="MGMT")
    route = ROUTES[IOS][1].format(vrf="MGMT")
    facts = derive(IOS, mgmt + route, mgmt + route.replace(".254", ".253"))
    text = "ip route vrf MGMT 0.0.0.0 0.0.0.0 10.0.0.25"
    assert facts.default_routes_touched == (
        DefaultRouteFact(text=f"{text}3", vrf="MGMT", negated=False),
        DefaultRouteFact(text=f"{text}4", vrf="MGMT", negated=True),
    )


def test_the_ios_view_reads_ip_vrf_forwarding() -> None:
    """The CISCO_IOS view gives the VRF of `ip vrf forwarding`."""
    running = (
        "interface Gi0/0\n ip vrf forwarding OLD\n ip address 10.0.0.1 255.255.255.0\n"
    )
    assert derive(IOS, running, running).mgmt_vrf == "OLD"


def test_a_deep_default_route_is_found() -> None:
    """A default route at the end of a deep lineage still matches."""
    chain = "".join(f"{' ' * depth}level {depth}\n" for depth in range(300))
    route = f"{chain}{' ' * 299} router static\n{' ' * 300} address-family ipv4 unicast"
    deep = f"{route}\n{' ' * 301} 0.0.0.0/0 192.0.2.1\n"
    assert derive("CISCO_XR", chain, deep).touches_mgmt_path
    assert not derive(
        "CISCO_XR", chain, deep.replace("0.0.0.0/0", "10.9.0.0/16")
    ).touches_mgmt_path


MGMT_ROUTE = "0.0.0.0/0 MgmtEth0/RP0/CPU0/0 10.0.0.254"


@pytest.mark.parametrize("vrf", ["", " vrf MGMT\n"])
def test_a_long_xr_default_route_is_found(vrf: str) -> None:
    """A route line longer than the prefix still gets its parents joined."""
    indent = " " * (vrf.count("\n") + 1)
    family = f"router static\n{vrf}{indent}address-family ipv4 unicast\n"
    route = f"{family}{indent} {MGMT_ROUTE}\n"
    assert derive("CISCO_XR", family, route).touches_mgmt_path
    changed = route.replace("10.0.0.254", "10.0.0.253")
    assert derive("CISCO_XR", route, changed).touches_mgmt_path


@pytest.mark.parametrize("platform_name", ["GENERIC", "JUNIPER_JUNOS", "VYOS"])
def test_a_platform_without_a_view_raises(platform_name: str) -> None:
    """A platform with no `hier_config` view gives `UnknownPlatformError`."""
    running_dump = parse(platform_name, "hostname r1\n")
    bundle = build_bundle(platform_name, running_dump, running_dump)
    device = DeviceRecord(
        id=DeviceID("d1"),
        name="r1",
        platform_name=platform_name,
        mgmt_address="10.0.0.1",
        metadata={},
    )
    with pytest.raises(UnknownPlatformError, match="No view registered"):
        config_derived(bundle, running_dump, device)


def test_a_default_gateway_change_outside_the_mgmt_vrf_is_not_the_path() -> None:
    """The view's default gateway is in the global table, not in the mgmt VRF."""
    mgmt = MGMT[IOS][1].format(vrf="MGMT")
    facts = derive(IOS, mgmt, f"{mgmt}ip default-gateway 10.0.0.254\n")
    assert facts.mgmt_vrf == "MGMT"
    assert not facts.touches_mgmt_path


TWO_HOLDERS = {
    IOS: "interface Gi0/0\n vrf forwarding CUST\n ip address 10.0.0.1 255.255.255.0\n"
    "interface Gi0/9\n vrf forwarding MGMT\n ip address 10.0.0.1 255.255.255.0\n",
    "ARISTA_EOS": "interface Ethernet1\n   vrf CUST\n   ip address 10.0.0.1/24\n"
    "interface Management1\n   vrf MGMT\n   ip address 10.0.0.1/24\n",
}


@pytest.mark.parametrize("platform_name", sorted(TWO_HOLDERS))
def test_holders_in_two_vrfs_make_the_mgmt_vrf_unknown(platform_name: str) -> None:
    """Every holder of the address counts, and two VRFs make `mgmt_vrf` None."""
    running = TWO_HOLDERS[platform_name]
    route = ROUTES[platform_name][1].format(vrf="MGMT")
    facts = derive(platform_name, running, running + route)
    assert facts.mgmt_vrf is None
    assert facts.mgmt_interface == min(MGMT_NAMES[platform_name])
    assert facts.touches_mgmt_path
    second = max(MGMT_NAMES[platform_name])
    moved = running.replace("MGMT", "OTHER")
    facts = derive(platform_name, running, moved)
    assert facts.interfaces_touched == (second,)
    assert facts.touches_mgmt_path


MGMT_NAMES = {IOS: ("Gi0/0", "Gi0/9"), "ARISTA_EOS": ("Ethernet1", "Management1")}
LONG_VRF = "V" * 300


@pytest.mark.parametrize("platform_name", ["CISCO_NXOS", "CISCO_XR"])
def test_a_cut_vrf_name_counts_for_the_mgmt_vrf(platform_name: str) -> None:
    """A VRF name that the parent cut shortens still matches the management VRF."""
    mgmt = MGMT[platform_name][1].format(vrf=LONG_VRF)
    route = ROUTES[platform_name][1].format(vrf=LONG_VRF)
    facts = derive(platform_name, mgmt, mgmt + route)
    assert facts.mgmt_vrf == LONG_VRF
    (fact,) = facts.default_routes_touched
    assert fact.vrf is not None
    assert LONG_VRF.startswith(fact.vrf)
    assert facts.touches_mgmt_path
    other = ROUTES[platform_name][1].format(vrf="CUST")
    assert not derive(platform_name, mgmt, mgmt + other).touches_mgmt_path
