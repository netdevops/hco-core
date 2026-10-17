"""The platform table names a safe-apply strategy and read-only commands."""

import pytest
from hier_config.models import Platform

from hco_core.domain.apply import ApplyStrategy
from hco_core.engine.platforms import PLATFORMS, PlatformProfile

V1_PLATFORMS = ("CISCO_IOS", "CISCO_NXOS", "CISCO_XR", "ARISTA_EOS")
WRITE_PREFIXES = ("configure", "write", "reload", "copy", "clear", "delete")
DEFAULT_ROUTE_PREFIXES = {
    "CISCO_IOS": ("ip route 0.0.0.0 0.0.0.0",),
    "CISCO_NXOS": ("ip route 0.0.0.0/0",),
    "CISCO_XR": ("address-family ipv4 unicast 0.0.0.0/0",),
    "ARISTA_EOS": ("ip route 0.0.0.0/0",),
}
SECTIONS = ("interface Gi1", "no ip access-list extended OLD", "router bgp 65000")


def every_command(profile: PlatformProfile) -> tuple[str, ...]:
    """Return every command that the profile can send."""
    sections = tuple(
        command for pair in profile.section_commands for command in pair[1]
    )
    return (profile.fingerprint_command, *profile.baseline_commands, *sections)


def test_table_covers_the_v1_platforms_by_platform_name() -> None:
    """Each key is a `Platform` name, never a value."""
    assert set(PLATFORMS) == set(V1_PLATFORMS)
    assert all(Platform[name].name == name for name in PLATFORMS)


@pytest.mark.parametrize(
    ("platform_name", "strategy"),
    [
        ("CISCO_IOS", ApplyStrategy.IOS_ARCHIVE_REPLACE),
        ("CISCO_NXOS", ApplyStrategy.NXOS_CHECKPOINT),
        ("CISCO_XR", ApplyStrategy.IOSXR_COMMIT_CONFIRMED),
        ("ARISTA_EOS", ApplyStrategy.EOS_SESSION_COMMIT_TIMER),
    ],
)
def test_each_platform_has_its_safe_apply_strategy(
    platform_name: str, strategy: ApplyStrategy
) -> None:
    """The strategy matches the safe-apply policy of the specification."""
    profile = PLATFORMS[platform_name]
    assert profile.strategy is strategy
    assert profile.fingerprint_command.startswith("show ")
    assert profile.baseline_commands
    assert profile.default_route_prefixes == DEFAULT_ROUTE_PREFIXES[platform_name]


@pytest.mark.parametrize("platform_name", V1_PLATFORMS)
def test_no_command_can_write_to_the_device(platform_name: str) -> None:
    """Every command is a `show` command and none starts with a write verb."""
    commands = every_command(PLATFORMS[platform_name])
    assert all(command.startswith("show ") for command in commands)
    assert not [c for c in commands if c.lower().startswith(WRITE_PREFIXES)]


@pytest.mark.parametrize("platform_name", V1_PLATFORMS)
def test_commands_for_adds_the_commands_of_each_section(platform_name: str) -> None:
    """The baseline comes first, then the commands of each touched section once."""
    profile = PLATFORMS[platform_name]
    commands = profile.commands_for(SECTIONS + SECTIONS)
    assert commands[: len(profile.baseline_commands)] == profile.baseline_commands
    assert len(commands) == len(set(commands))
    assert len(commands) > len(profile.baseline_commands)


def test_commands_for_a_negated_section_adds_its_commands() -> None:
    """A removed ACL adds the ACL command, after the baseline."""
    profile = PLATFORMS["CISCO_IOS"]
    assert profile.commands_for(("no ip access-list extended OLD",)) == (
        *profile.baseline_commands,
        "show access-lists",
    )


def test_commands_for_no_section_is_the_baseline() -> None:
    """A section without commands adds nothing, and a prefix must end at a word."""
    profile = PLATFORMS["CISCO_IOS"]
    assert profile.commands_for(()) == profile.baseline_commands
    assert profile.commands_for(("hostname r1", "interfaces")) == (
        profile.baseline_commands
    )


@pytest.mark.parametrize(
    "command",
    [
        "configure terminal",
        " write memory",
        "Reload",
        "show running-config | redirect flash:x",
        "show running-config | include a | tee flash:x",
        "show running-config > bootflash:x",
        "show clock\nreload",
        "show clock\rreload",
        "show clock ; reload",
        "show running-config | include x\nwrite erase",
        "show clock\x00",
        "show cl\u00f6ck",
    ],
)
def test_profile_refuses_a_write_command(command: str) -> None:
    """A profile with a write command cannot exist."""
    with pytest.raises(ValueError, match="read-only"):
        PlatformProfile(
            strategy=ApplyStrategy.HCO_TIMED_REVERT,
            fingerprint_command="show clock",
            baseline_commands=(command,),
        )
