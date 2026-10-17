"""Facts about what a remediation touches, from `hier_config` views.

The facts carry no risk judgement. The caller decides what each fact means.
"""

import re
from collections.abc import Iterable, Mapping
from functools import cache
from ipaddress import IPv4Address, ip_interface

from hier_config import HConfig, HConfigChild
from hier_config.constructors import get_hconfig_view
from hier_config.platforms.view_base import ConfigViewInterface, HConfigView

from hco_core.domain.device import DeviceRecord
from hco_core.domain.remediation import (
    ConfigDerived,
    DefaultRouteFact,
    RemediationBundle,
)
from hco_core.engine.errors import engine_errors
from hco_core.engine.platforms import PLATFORMS
from hco_core.engine.remediation import load_dump

_VRF_PLACEHOLDER = "{vrf}"
_VRF_NAME_CHARACTERS = 128
"""The longest VRF name that a parent line can hold and still match."""
_INTERFACE_VRF = re.compile(r"^(?:ip )?vrf (?:forwarding |member )?(\S+)$")


def config_derived(
    bundle: RemediationBundle,
    running_dump: Mapping[str, object],
    device: DeviceRecord,
) -> ConfigDerived:
    """Return the facts of the bundle's remediation against the running config.

    A management address that is not an IPv4 address matches no interface.
    A platform outside `PLATFORMS` has no default route prefixes. A platform
    with no `hier_config` view raises `UnknownPlatformError`; only ARISTA_EOS,
    ARUBA_AOSCX, CISCO_IOS, CISCO_NXOS, CISCO_XR, and HP_PROCURVE have a view.
    A malformed dump raises `InvalidConfigError`.
    """
    platform_name = bundle.platform_name
    with engine_errors():
        running = load_dump(platform_name, running_dump)
        remediation = load_dump(platform_name, bundle.remediation_dump)
        future = load_dump(platform_name, bundle.future_dump)
        views = (get_hconfig_view(running), get_hconfig_view(future))
        negation = running.driver.negation_prefix
        lines = tuple(remediation.all_children())
        sections = tuple(child.text for child in remediation.children)
        section_texts = {text.removeprefix(negation) for text in sections}
        touched = [
            view
            for config_view in views
            for view in config_view.interface_views
            if view.config.text in section_texts
        ]
        unused_before = _texts(running.unused_objects())
        unused_after = _texts(future.unused_objects())
        mgmt = _mgmt_view(views[0].interface_views, device.mgmt_address)
        mgmt_vrf = _vrf_of(mgmt) if mgmt else None
        routes = _default_routes_touched(platform_name, (remediation, running, future))
        return ConfigDerived(
            interfaces_touched=_sorted(view.name for view in touched),
            bundles_touched=_sorted(_bundle_names(touched)),
            vlans_touched=_vlans_touched(views, touched, section_texts),
            nac_interfaces_touched=_sorted(v.name for v in touched if v.has_nac),
            unused_objects_before=unused_before,
            unused_objects_after=unused_after,
            unused_objects_created=_sorted(set(unused_after) - set(unused_before)),
            negation_count=sum(line.text.startswith(negation) for line in lines),
            section_negations=_sorted(t for t in sections if t.startswith(negation)),
            command_count=len(lines),
            remediation_tags=_sorted(remediation.tags),
            top_level_sections=_sorted(sections),
            mgmt_interface=mgmt.name if mgmt else None,
            mgmt_vrf=mgmt_vrf,
            default_routes_touched=routes,
            touches_mgmt_path=(
                mgmt is not None and mgmt.name in {v.name for v in touched}
            )
            or (
                mgmt_vrf is None
                and views[0].ipv4_default_gw != views[1].ipv4_default_gw
            )
            or any(mgmt_vrf in {None, route.vrf} for route in routes),
        )


def _sorted[T: (str, int)](items: Iterable[T]) -> tuple[T, ...]:
    return tuple(sorted(set(items)))


def _texts(children: Iterable[HConfigChild]) -> tuple[str, ...]:
    return _sorted(child.text for child in children)


def _bundle_names(views: Iterable[ConfigViewInterface]) -> list[str]:
    names = [view.name for view in views if view.is_bundle]
    return names + [view.bundle_name for view in views if view.bundle_name]


def _vlans_touched(
    views: tuple[HConfigView, HConfigView],
    touched: Iterable[ConfigViewInterface],
    sections: set[str],
) -> tuple[int, ...]:
    """Return VLANs that change, that a touched section defines, or that a port uses."""
    before, after = ({(vlan.id, vlan.name) for vlan in view.vlans} for view in views)
    ids = [vlan_id for vlan_id, _ in before ^ after]
    ids += [vlan_id for vlan_id, _ in before | after if f"vlan {vlan_id}" in sections]
    ids += [vlan for view in touched for vlan in view.tagged_vlans]
    ids += [view.native_vlan for view in touched if view.native_vlan]
    return _sorted(ids)


def _mgmt_view(
    views: Iterable[ConfigViewInterface], address: str
) -> ConfigViewInterface | None:
    """Return the first interface by name that holds the IPv4 address, or None."""
    try:
        wanted = ip_interface(address).ip
    except ValueError:
        return None
    if not isinstance(wanted, IPv4Address):
        return None
    holders = (
        view
        for view in views
        if any(found.ip == wanted for found in view.ipv4_interfaces)
    )
    return min(holders, key=lambda view: view.name, default=None)


def _vrf_of(view: ConfigViewInterface) -> str | None:
    """Return the VRF of the interface from the view, else from a child line.

    The CISCO_IOS view gives an empty VRF for `vrf forwarding <name>`.
    """
    if view.vrf:
        return view.vrf
    names = (_INTERFACE_VRF.match(child.text) for child in view.config.children)
    return next((name[1] for name in names if name), None)


def _default_routes_touched(
    platform_name: str, configs: tuple[HConfig, HConfig, HConfig]
) -> tuple[DefaultRouteFact, ...]:
    """Return the default routes in the remediation and those the change adds or removes."""
    profile = PLATFORMS.get(platform_name)
    prefixes = profile.default_route_prefixes if profile else ()
    in_remediation, in_running, in_future = (
        _default_routes(config, prefixes) for config in configs
    )
    facts = {(text, vrf, negated) for (text, vrf), negated in in_remediation.items()}
    facts |= {(*route, True) for route in in_running.keys() - in_future.keys()}
    facts |= {(*route, False) for route in in_future.keys() - in_running.keys()}
    return tuple(
        DefaultRouteFact(text=text, vrf=vrf, negated=negated)
        for text, vrf, negated in sorted(facts, key=lambda f: (f[1] or "", f[0], f[2]))
    )


@cache
def _patterns(prefixes: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Return a pattern for each prefix, with `{vrf}` as the `vrf` group."""
    placeholder = re.escape(_VRF_PLACEHOLDER)
    return tuple(
        re.compile(re.escape(prefix).replace(placeholder, r"(?P<vrf>\S+)"))
        for prefix in prefixes
    )


def _default_routes(
    config: HConfig, prefixes: tuple[str, ...]
) -> dict[tuple[str, str | None], bool]:
    """Return each default route by its text and VRF, and if the line is negated.

    A match joins the texts of some parents and the line. The longest match
    wins, so a VRF form beats the global form. The parents are cut to the
    width of the longest prefix and limited to its word count, so the work is
    linear.
    """
    if not prefixes:
        return {}
    patterns = _patterns(prefixes)
    negation = config.driver.negation_prefix
    most_parents = max(prefix.count(" ") for prefix in prefixes)
    width = max(map(len, prefixes)) + _VRF_NAME_CHARACTERS
    open_texts: list[str] = []
    found: dict[tuple[str, str | None], bool] = {}
    for line in config.all_children():
        del open_texts[line.depth - 1 :]
        text = line.text.removeprefix(negation)
        parents = open_texts[-most_parents:] if most_parents else []
        open_texts.append(text[:width])
        match = None
        for start in range(len(parents) + 1):
            joined = " ".join((*parents[start:], text))
            match = next(filter(None, (p.match(joined) for p in patterns)), None)
            if match:
                break
        if match:
            vrf = match.groupdict().get("vrf")
            found[match.string, vrf] = line.text.startswith(negation)
    return found
