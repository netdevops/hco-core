"""The validation gate blocks the adversarial corpus and signs only a passed plan."""

import hashlib
import hmac
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from hco_core.domain.remediation import DumpJson
from hco_core.domain.validation import ValidationOutcome
from hco_core.engine.errors import InvalidConfigError, UnknownPlatformError
from hco_core.engine.remediation import build_bundle, parse, render_lines
from hco_core.engine.validation import validate_plan, verify_token

CONFIGS = Path(__file__).resolve().parent.parent / "fixtures" / "configs"
PLATFORM_NAMES = ("CISCO_IOS", "CISCO_NXOS", "CISCO_XR", "ARISTA_EOS")
IOS = "CISCO_IOS"
KEY = b"k" * 32
ACL = "ip access-list extended CUST-IN"
HTTPS = "10 permit tcp 198.51.100.0 0.0.0.255 any eq 443"
SSH_20 = "20 permit tcp 198.51.100.0 0.0.0.255 any eq 22"
DNS_20 = "20 permit udp 198.51.100.0 0.0.0.255 any eq 53"
SSH_30 = "30 permit tcp 198.51.100.0 0.0.0.255 any eq 22"
DNS_30 = "30 permit udp 198.51.100.0 0.0.0.255 any eq 53"
UPLINK = "interface GigabitEthernet0/0/1"
SPARE = "interface GigabitEthernet0/0/2"
RUNNING = parse(
    IOS,
    f"hostname r1\n{ACL}\n {HTTPS}\n {SSH_20}\n"
    f"{UPLINK}\n ip access-group CUST-IN in\n{SPARE}\n",
)
INTENDED = parse(
    IOS,
    f"hostname r1\n{ACL}\n {HTTPS}\n {DNS_20}\n {SSH_30}\n"
    f"{UPLINK}\n ip access-group CUST-IN in\n description uplink\n{SPARE}\n",
)
ACL_ONLY_INTENDED = parse(
    IOS,
    f"hostname r1\n{ACL}\n {HTTPS}\n {SSH_20}\n {DNS_30}\n"
    f"{UPLINK}\n ip access-group CUST-IN in\n{SPARE}\n",
)
REMEDIATION = render_lines(IOS, build_bundle(IOS, RUNNING, INTENDED).remediation_dump)
ACL_CHANGE = (ACL, f" no {SSH_20}", f" {DNS_20}", f" {SSH_30}")
DESCRIPTION = (UPLINK, " description uplink")


def resequence(scaffold: str, removal: str) -> tuple[str, ...]:
    """Return the ACL resequence plan of spec 08 with one scaffolding pair."""
    return (
        ACL,
        f" {scaffold}",
        " no 10",
        " no 20",
        f" {HTTPS}",
        f" {DNS_20}",
        f" {SSH_30}",
        f" {removal}",
        *DESCRIPTION,
    )


def validate(
    plan: tuple[str, ...],
    intended: DumpJson = INTENDED,
    forbidden: tuple[re.Pattern[str], ...] = (),
) -> ValidationOutcome:
    """Validate a plan against the IOS running config."""
    return validate_plan(
        plan, RUNNING, intended, platform_name=IOS, forbidden=forbidden, key=KEY
    )


def test_the_deterministic_remediation_is_the_expected_plan() -> None:
    """Guard the corpus: the plans below are built from this remediation."""
    assert (
        ACL,
        f"  no {SSH_20}",
        f"  {DNS_20}",
        f"  {SSH_30}",
        UPLINK,
        "  description uplink",
    ) == REMEDIATION


@pytest.mark.parametrize(
    ("plan", "intended", "check"),
    [
        pytest.param((), INTENDED, 1, id="empty plan"),
        pytest.param(("hostname\tr1",), INTENDED, 1, id="tab"),
        pytest.param(("\tdescription x",), INTENDED, 1, id="leading tab"),
        pytest.param(("hostname r1\nreload",), INTENDED, 1, id="newline"),
        pytest.param(("hostname ré",), INTENDED, 1, id="not ascii"),
        pytest.param(("hostname r1", "   "), INTENDED, 1, id="blank line"),
        pytest.param((*DESCRIPTION, *DESCRIPTION), INTENDED, 2, id="repeated section"),
        pytest.param((*REMEDIATION, "end"), INTENDED, 2, id="line the parser drops"),
        pytest.param(
            (UPLINK, " description  uplink"), INTENDED, 2, id="line rewritten"
        ),
        pytest.param(("relo",), INTENDED, 3, id="relo"),
        pytest.param(("wr era",), INTENDED, 3, id="wr era"),
        pytest.param(("do reload",), INTENDED, 3, id="do reload"),
        pytest.param(("DO RELOAD",), INTENDED, 3, id="upper case"),
        pytest.param(("do write erase",), INTENDED, 3, id="do write erase"),
        pytest.param(("crypto key zeroize rsa",), INTENDED, 3, id="zeroize"),
        pytest.param(("format flash:",), INTENDED, 3, id="format"),
        pytest.param(("request system reboot",), INTENDED, 3, id="request system"),
        pytest.param((UPLINK, " reload in 5"), INTENDED, 3, id="nested reload"),
        pytest.param(
            (*REMEDIATION, "no ip route 192.0.2.0 255.255.255.0 198.51.100.1"),
            INTENDED,
            4,
            id="unresolved negation",
        ),
        pytest.param(ACL_CHANGE, INTENDED, 5, id="line missing"),
        pytest.param(
            (*REMEDIATION, "ip route 192.0.2.0 255.255.255.0 198.51.100.1"),
            INTENDED,
            5,
            id="line unwanted",
        ),
        pytest.param(
            (*ACL_CHANGE, SPARE, " description uplink"),
            INTENDED,
            6,
            id="line set passes but fixed point fails",
        ),
        pytest.param(
            (*REMEDIATION, "ip route 0.0.0.0 0.0.0.0 198.51.100.66", "no ip"),
            INTENDED,
            7,
            id="fake scaffolding no ip",
        ),
        pytest.param(
            (
                *ACL_CHANGE,
                *DESCRIPTION,
                " ip access-group CUST-IN in",
                " no ip access-group CUST-IN in",
            ),
            INTENDED,
            7,
            id="scaffolding removes a running line",
        ),
        pytest.param(
            (ACL, " 10 permit ip any any", " no 10", *ACL_CHANGE[1:], *DESCRIPTION),
            INTENDED,
            7,
            id="scaffolding removes a running sequence number",
        ),
        pytest.param(
            (*REMEDIATION, SPARE, " 5 permit ip any any", " no 5"),
            INTENDED,
            7,
            id="permit outside an access list",
        ),
        pytest.param(
            (ACL, " 5 permit ip any any", f" {DNS_30}", " no 5"),
            ACL_ONLY_INTENDED,
            7,
            id="permit in an access list that is not resequenced",
        ),
        pytest.param(
            resequence("15 permit ip any any", "no 15"),
            INTENDED,
            7,
            id="permit above a retained entry",
        ),
        pytest.param(
            resequence("permit ip any any", "no permit ip any any"),
            INTENDED,
            2,
            id="unnumbered entry that the IOS driver numbers",
        ),
    ],
)
def test_the_adversarial_corpus_is_blocked_at_its_check(
    plan: tuple[str, ...], intended: DumpJson, check: int
) -> None:
    """Each plan stops at the first check that it fails, with no token."""
    outcome = validate(plan, intended)
    assert outcome.failed_check == check, outcome.problems
    assert not outcome.ok
    assert outcome.problems
    assert outcome.token is None


@pytest.mark.parametrize(
    ("plan", "scaffolding"),
    [
        pytest.param(REMEDIATION, (), id="deterministic remediation"),
        pytest.param(
            resequence("5 permit ip any any", "no 5"),
            (" 5 permit ip any any", " no 5"),
            id="acl resequence with permit-any scaffolding",
        ),
        pytest.param(
            (*REMEDIATION, SPARE, " description temp", " no description temp"),
            (" description temp", " no description temp"),
            id="exact scaffolding",
        ),
    ],
)
def test_a_converging_plan_passes_with_a_token(
    plan: tuple[str, ...], scaffolding: tuple[str, ...]
) -> None:
    """A passed plan names its scaffolding and verifies with its token."""
    outcome = validate(plan)
    assert outcome.ok, outcome.problems
    assert outcome.scaffolding == scaffolding
    assert outcome.token is not None
    assert verify_token(plan, outcome.token, KEY)


def test_negations_are_listed_for_review() -> None:
    """The review list holds every line that starts with the negation prefix."""
    assert validate(REMEDIATION).commands_requiring_review == (f"  no {SSH_20}",)


def test_a_forbidden_pattern_blocks_at_check_8() -> None:
    """The caller's forbidden patterns apply after the convergence checks."""
    outcome = validate(REMEDIATION, forbidden=(re.compile(r"description\s"),))
    assert outcome.failed_check == 8  # ruff: ignore[magic-value-comparison]
    assert outcome.problems == ("forbidden: '  description uplink'",)


@pytest.mark.parametrize("platform_name", PLATFORM_NAMES)
def test_the_fixture_remediation_passes_on_each_platform(platform_name: str) -> None:
    """The deterministic remediation of every v1 fixture passes the gate."""
    folder = CONFIGS / platform_name.lower()
    running = parse(platform_name, (folder / "running.txt").read_text("utf8"))
    intended = parse(platform_name, (folder / "intended.txt").read_text("utf8"))
    bundle = build_bundle(platform_name, running, intended)
    plan = render_lines(platform_name, bundle.remediation_dump)
    outcome = validate_plan(
        plan, running, intended, platform_name=platform_name, forbidden=(), key=KEY
    )
    assert outcome.ok, outcome.problems
    assert outcome.token is not None
    assert verify_token(plan, outcome.token, KEY)


def test_the_token_is_an_hmac_of_the_canonical_plan_bytes() -> None:
    """The canonical form is the compact JSON of a version tag and the lines."""
    canonical = json.dumps(
        ["hco-core/validation/v1", list(REMEDIATION)], separators=(",", ":")
    ).encode("ascii")
    expected = hmac.new(KEY, canonical, hashlib.sha256).hexdigest()
    assert validate(REMEDIATION).token == expected


@pytest.mark.parametrize(
    ("plan", "key"),
    [
        pytest.param((*REMEDIATION[:-1], "  description evil"), KEY, id="line changed"),
        pytest.param((*REMEDIATION, "reload"), KEY, id="line added"),
        pytest.param(REMEDIATION[:-1], KEY, id="line removed"),
        pytest.param(tuple(reversed(REMEDIATION)), KEY, id="lines reordered"),
        pytest.param(
            (*REMEDIATION[:-2], "\n".join(REMEDIATION[-2:])), KEY, id="joined"
        ),
        pytest.param(REMEDIATION, b"x" * 32, id="other key"),
    ],
)
def test_a_changed_plan_or_key_does_not_verify(
    plan: tuple[str, ...], key: bytes
) -> None:
    """The token binds the exact lines, their order, and the key."""
    token = validate(REMEDIATION).token
    assert token is not None
    assert not verify_token(plan, token, key)


@pytest.mark.parametrize("token", ["", "00", "é" * 64])
def test_a_malformed_token_does_not_verify(token: str) -> None:
    """A token that is not the digest fails without an error."""
    assert not verify_token(REMEDIATION, token, KEY)


def test_a_short_key_is_refused() -> None:
    """A key shorter than the 32-byte digest is a caller error."""
    with pytest.raises(ValueError, match="32 bytes"):
        validate_plan(
            REMEDIATION, RUNNING, INTENDED, platform_name=IOS, forbidden=(), key=b""
        )
    with pytest.raises(ValueError, match="32 bytes"):
        verify_token(REMEDIATION, "00", b"short")


def test_a_malformed_config_raises_an_engine_error() -> None:
    """A bad running or intended dump is a caller error, not a blocked plan."""
    bad: DumpJson = {
        "lines": [
            {
                "depth": 2,
                "text": "x",
                "tags": [],
                "comments": [],
                "new_in_config": False,
            }
        ]
    }
    with pytest.raises(InvalidConfigError):
        validate_plan(
            REMEDIATION, bad, INTENDED, platform_name=IOS, forbidden=(), key=KEY
        )
    with pytest.raises(UnknownPlatformError):
        validate_plan(
            REMEDIATION, RUNNING, INTENDED, platform_name="NOPE", forbidden=(), key=KEY
        )


@pytest.mark.parametrize(
    ("failed_check", "problems", "token"),
    [(None, (), None), (None, ("x",), "t"), (3, ("x",), "t"), (3, (), None)],
)
def test_an_inconsistent_outcome_is_refused(
    failed_check: int | None, problems: tuple[str, ...], token: str | None
) -> None:
    """Only a passed outcome carries a token, and only a blocked one a problem."""
    with pytest.raises(ValidationError):
        ValidationOutcome(failed_check=failed_check, problems=problems, token=token)
