"""Facts about what a remediation touches, from `hier_config` views.

The facts carry no risk judgement. The caller decides what each fact means.
"""

from collections.abc import Iterable, Mapping
from ipaddress import IPv4Address, ip_interface

from hier_config import HConfig, HConfigChild
from hier_config.constructors import get_hconfig_view
from hier_config.platforms.view_base import ConfigViewInterface, HConfigView

from hco_core.domain.device import DeviceRecord
from hco_core.domain.remediation import ConfigDerived, RemediationBundle
from hco_core.engine.errors import engine_errors
from hco_core.engine.platforms import PLATFORMS
from hco_core.engine.remediation import load_dump


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
            touches_mgmt_path=bool(
                _interfaces_with(views[0].interface_views, device.mgmt_address)
                & {view.name for view in touched}
            )
            or views[0].ipv4_default_gw != views[1].ipv4_default_gw
            or _touches_default_route(platform_name, (remediation, running, future)),
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


def _interfaces_with(views: Iterable[ConfigViewInterface], address: str) -> set[str]:
    try:
        wanted = ip_interface(address).ip
    except ValueError:
        return set()
    if not isinstance(wanted, IPv4Address):
        return set()
    return {
        view.name
        for view in views
        if any(found.ip == wanted for found in view.ipv4_interfaces)
    }


def _touches_default_route(
    platform_name: str, configs: tuple[HConfig, HConfig, HConfig]
) -> bool:
    """Return True if the remediation has a default route or the routes differ."""
    profile = PLATFORMS.get(platform_name)
    prefixes = profile.default_route_prefixes if profile else ()
    in_remediation, in_running, in_future = (
        _default_routes(config, prefixes) for config in configs
    )
    return bool(in_remediation) or in_running != in_future


def _default_routes(config: HConfig, prefixes: tuple[str, ...]) -> set[str]:
    """Return the paths of the lines where a default route prefix match ends.

    A match starts at a line or a parent and joins texts down to the line. The
    joined parent texts stop at the length of the longest prefix, so the work
    is linear.
    """
    if not prefixes:
        return set()
    negation = config.driver.negation_prefix
    longest = max(map(len, prefixes))
    open_texts: list[str] = []
    found: set[str] = set()
    for line in config.all_children():
        del open_texts[line.depth - 1 :]
        open_texts.append(line.text.removeprefix(negation))
        joined = open_texts[-1][:longest]
        parents = ""
        for text in (*reversed(open_texts[:-1]), None):
            if joined.startswith(prefixes):
                found.add("\n".join(open_texts))
                break
            if text is None or len(parents) >= longest:
                break
            parents = f"{text} {parents}"
            joined = f"{text} {joined}"
    return found
