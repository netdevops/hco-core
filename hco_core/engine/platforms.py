"""The safe-apply strategy and the read-only commands of each platform."""

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Self

from hier_config.registry import get_hconfig_driver
from pydantic import model_validator

from hco_core.domain.apply import ApplyStrategy
from hco_core.domain.base import BaseModel
from hco_core.engine.errors import engine_errors

type SectionCommands = tuple[tuple[str, tuple[str, ...]], ...]

_OUTPUT_FILTERS = ("include ", "exclude ", "begin ", "section ")
"""The only verbs permitted after a pipe. Others, such as `redirect`, write files."""


def _refusal(command: str) -> str | None:
    """Return the read-only rule that the command breaks, or None."""
    show, *filters = command.split("|")
    if not (command.isascii() and command.isprintable()):
        return "it must be printable ASCII"
    if {";", ">"} & set(command):
        return "it must not contain ';' or '>'"
    if not show.startswith("show "):
        return "it must start with 'show '"
    if not all(part.strip().startswith(_OUTPUT_FILTERS) for part in filters):
        return "only include, exclude, begin, or section may follow after a pipe"
    return None


class PlatformProfile(BaseModel):
    """What HCO may send to one platform, and how it applies a change.

    Every command must be printable ASCII and start with `show `, with no `;`,
    no `>`, and only output filters after a pipe. The model refuses any other
    command, because a collection command must never change the device.
    """

    strategy: ApplyStrategy
    fingerprint_command: str
    baseline_commands: tuple[str, ...]
    section_commands: SectionCommands = ()
    """Pairs of a top-level section prefix and the commands it adds."""
    default_route_prefixes: tuple[str, ...] = ()
    """Prefixes of an IPv4 default route, matched against a line and its parents.

    `hco_core.engine.views` joins the texts from a parent down to the line, so
    a prefix can include the parent line of a nested route. `{vrf}` stands for
    one VRF name.
    """

    @model_validator(mode="after")
    def _only_show_commands(self) -> Self:
        sections = (
            command for _, commands in self.section_commands for command in commands
        )
        for command in (self.fingerprint_command, *self.baseline_commands, *sections):
            if rule := _refusal(command):
                message = f"{command!r} is not a read-only show command: {rule}"
                raise ValueError(message)
        return self

    def commands_for(self, top_level_sections: Iterable[str]) -> tuple[str, ...]:
        """Return the baseline, then the commands of each touched section, once each.

        A section matches a prefix when its text, without a leading `no `, is the
        prefix or starts with the prefix and a space.
        """
        commands = dict.fromkeys(self.baseline_commands)
        for section in top_level_sections:
            text = section.removeprefix("no ")
            for prefix, section_commands in self.section_commands:
                if text == prefix or text.startswith(f"{prefix} "):
                    commands.update(dict.fromkeys(section_commands))
        return tuple(commands)


_INTERFACE_COMMANDS = ("show interfaces status", "show interfaces description")
_LAST_CHANGE = "show running-config | include Last configuration change"

PLATFORMS: Mapping[str, PlatformProfile] = MappingProxyType(
    {
        "CISCO_IOS": PlatformProfile(
            strategy=ApplyStrategy.IOS_ARCHIVE_REPLACE,
            fingerprint_command=_LAST_CHANGE,
            baseline_commands=(
                "show version",
                "show clock",
                "show users",
                "show logging",
            ),
            section_commands=(
                ("interface", _INTERFACE_COMMANDS),
                ("router bgp", ("show ip bgp summary",)),
                ("router ospf", ("show ip ospf neighbor",)),
                ("ip access-list", ("show access-lists",)),
                ("access-list", ("show access-lists",)),
                ("ip route", ("show ip route static",)),
                ("vlan", ("show vlan brief",)),
            ),
            default_route_prefixes=(
                "ip route 0.0.0.0 0.0.0.0",
                "ip route vrf {vrf} 0.0.0.0 0.0.0.0",
            ),
        ),
        "CISCO_NXOS": PlatformProfile(
            strategy=ApplyStrategy.NXOS_CHECKPOINT,
            fingerprint_command=_LAST_CHANGE,
            baseline_commands=(
                "show version",
                "show clock",
                "show users",
                "show logging last 50",
            ),
            section_commands=(
                ("interface", ("show interface status", "show interface description")),
                ("router bgp", ("show ip bgp summary",)),
                ("router ospf", ("show ip ospf neighbors",)),
                ("ip access-list", ("show ip access-lists",)),
                ("ip route", ("show ip route static",)),
                ("vlan", ("show vlan brief",)),
            ),
            default_route_prefixes=(
                "ip route 0.0.0.0/0",
                "vrf context {vrf} ip route 0.0.0.0/0",
            ),
        ),
        "CISCO_XR": PlatformProfile(
            strategy=ApplyStrategy.IOSXR_COMMIT_CONFIRMED,
            fingerprint_command="show configuration commit list 1",
            baseline_commands=(
                "show version",
                "show clock",
                "show users",
                "show logging last 50",
            ),
            section_commands=(
                ("interface", ("show interfaces brief", "show interfaces description")),
                ("router bgp", ("show bgp summary",)),
                ("router ospf", ("show ospf neighbor",)),
                ("ipv4 access-list", ("show access-lists ipv4",)),
                ("router static", ("show route static",)),
            ),
            default_route_prefixes=(
                "router static address-family ipv4 unicast 0.0.0.0/0",
                "router static vrf {vrf} address-family ipv4 unicast 0.0.0.0/0",
            ),
        ),
        "ARISTA_EOS": PlatformProfile(
            strategy=ApplyStrategy.EOS_SESSION_COMMIT_TIMER,
            fingerprint_command=_LAST_CHANGE,
            baseline_commands=(
                "show version",
                "show clock",
                "show users",
                "show logging",
            ),
            section_commands=(
                ("interface", _INTERFACE_COMMANDS),
                ("router bgp", ("show ip bgp summary",)),
                ("router ospf", ("show ip ospf neighbor",)),
                ("ip access-list", ("show ip access-lists",)),
                ("ip route", ("show ip route static",)),
                ("vlan", ("show vlan brief",)),
            ),
            default_route_prefixes=(
                "ip route 0.0.0.0/0",
                "ip route vrf {vrf} 0.0.0.0/0",
            ),
        ),
    }
)
"""The four version 1 platforms, by `Platform` name."""


def canonical_platform_name(name: str) -> str:
    """Return the registered platform name in upper case, as `Platform` names are.

    The `hier_config` registry finds a name in any case. A name with no
    registered driver raises `UnknownPlatformError`.
    """
    with engine_errors():
        get_hconfig_driver(name)
    return name.upper()
