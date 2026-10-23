# Engine

`hco_core.engine` is the only package that imports `hier_config`. Its functions take and return `Dump` JSON, never rendered text. Each error is an `EngineError` subclass, never a `hier_config` error.

## Remediation

```python
from hco_core.engine.fingerprints import change_fingerprint
from hco_core.engine.remediation import build_bundle, parse, render_lines

running = parse("CISCO_IOS", "interface Gi1/0/1\n shutdown\n")
intended = parse("CISCO_IOS", "interface Gi1/0/1\n no shutdown\nno ip http server\n")
bundle = build_bundle("CISCO_IOS", running, intended)

assert render_lines("CISCO_IOS", bundle.remediation_dump) == (
    "interface Gi1/0/1",
    "  no shutdown",
    "no ip http server",
)
(negation,) = bundle.future_report.unresolved_negations
assert negation.text == "no ip http server"
assert len(change_fingerprint(bundle.remediation_dump)) == 64
```

## Config-derived facts and the platform table

`config_derived()` reports what a remediation touches. It gives facts only. The caller decides the risk of each fact.

```python
from hco_core.domain.device import DeviceRecord
from hco_core.domain.ids import DeviceID
from hco_core.engine.platforms import PLATFORMS
from hco_core.engine.remediation import build_bundle, parse
from hco_core.engine.views import config_derived

running = parse("CISCO_IOS", "interface Gi0/0\n ip address 10.0.0.1 255.255.255.0\n")
intended = parse("CISCO_IOS", "interface Gi0/0\n ip address 10.0.0.2 255.255.255.0\n")
bundle = build_bundle("CISCO_IOS", running, intended)
device = DeviceRecord(
    id=DeviceID("r1"),
    name="r1",
    platform_name="CISCO_IOS",
    mgmt_address="10.0.0.1",
)

facts = config_derived(bundle, running, device)
assert facts.interfaces_touched == ("Gi0/0",)
assert facts.mgmt_interface == "Gi0/0"
assert facts.touches_mgmt_path

profile = PLATFORMS["CISCO_IOS"]
assert profile.strategy == "ios_archive_replace"
assert "show interfaces status" in profile.commands_for(facts.top_level_sections)
```

### The management path

- `mgmt_interface` is the first running interface, by name, that holds the management address.
- Every interface that holds the address counts for `mgmt_vrf` and `touches_mgmt_path`.
- `mgmt_vrf` is the VRF of these interfaces. It is `None` for the global table, when no interface holds the address, or when the interfaces are in different VRFs.
- `touches_mgmt_path` is true when the change touches an interface that holds the management address. It is also true when the change touches a default route in `mgmt_vrf`.
- When `mgmt_vrf` is `None`, any default route counts. A change of the default gateway of the view also counts.
- The management address must be an IPv4 address. A host name or an IPv6 address matches no interface.
- On `CISCO_IOS`, the `hier_config` view gives an empty VRF for `vrf forwarding MGMT`. `mgmt_vrf` therefore comes from the `vrf forwarding`, `ip vrf forwarding`, `vrf member`, or `vrf` child line of the interface.

### Default routes

- `default_routes_touched` lists each IPv4 default route that the change adds or removes, with its text and its VRF.
- The `default_route_prefixes` of each platform hold the global form and the VRF form, such as `ip route vrf {vrf} 0.0.0.0/0` on `ARISTA_EOS`. The longest match wins.
- The route lines find the default routes, not `ipv4_default_gw` of the view. On `CISCO_IOS`, the view reads `ip default-gateway` only.
- hco-core cuts a parent line after the length of the longest prefix plus 128 characters. A long VRF name in a parent line can therefore be cut. A route VRF of 128 characters or more counts for the management path when `mgmt_vrf` starts with it.
- A platform outside `PLATFORMS` has no default route prefixes.

### Limits

- hco-core does not find an IPv6 default route, a default route from a routing protocol, or a route that leaks from one VRF to another.
- A change that removes `address-family ipv4` from the management VRF definition cuts the management path. An example is `vrf definition MGMT` on `CISCO_IOS`. hco-core does not find this change: `touches_mgmt_path` is false and `default_routes_touched` is empty.
- `config_derived()` needs a `hier_config` view. Only `ARISTA_EOS`, `ARUBA_AOSCX`, `CISCO_IOS`, `CISCO_NXOS`, `CISCO_XR`, and `HP_PROCURVE` have one. For other platforms it raises `UnknownPlatformError`, but `build_bundle()` works.
- In `hier_config` 4.0.0b4, the four built-in drivers have no unused-object rules. `unused_objects_before`, `unused_objects_after`, and `unused_objects_created` are therefore empty for these platforms.

### The command allowlist

Every command in the platform table is a `show` command in printable ASCII. A profile refuses any other command, `;`, and any output redirection. After a pipe, only `include`, `exclude`, `begin`, and `section` are permitted.

## The validation gate

`validate_plan()` is the single gate. Every device write must pass it. It signs only a plan that passes all nine checks. `DeviceSession.apply()` must refuse a plan whose token does not verify.

```python
import re
import secrets

from hco_core.domain.risk import RiskTier
from hco_core.engine.freshness import freshness_verdict
from hco_core.engine.remediation import build_bundle, parse, render_lines
from hco_core.engine.validation import validate_plan, verify_token

running = parse("CISCO_IOS", "interface Gi0/1\n description old\n")
intended = parse("CISCO_IOS", "interface Gi0/1\n description new\n")
bundle = build_bundle("CISCO_IOS", running, intended)
plan = render_lines("CISCO_IOS", bundle.remediation_dump)
key = secrets.token_bytes(32)
forbidden = (re.compile(r"^\s*username\s"),)

outcome = validate_plan(
    plan, running, intended, platform_name="CISCO_IOS", forbidden=forbidden, key=key
)
assert outcome.ok
assert outcome.token is not None
assert verify_token(
    plan, outcome.token, key, platform_name="CISCO_IOS", running=running
)

blocked = validate_plan(
    ("do reload",), running, intended, platform_name="CISCO_IOS", forbidden=(), key=key
)
assert blocked.failed_check == 3
assert blocked.token is None

assert freshness_verdict(RiskTier.MEDIUM, "fp-1", "fp-1") == "use_snapshot"
assert freshness_verdict(RiskTier.HIGH, "fp-1", "fp-1") == "recollect"
```

The checks run in this order. The first check that fails blocks the plan:

1. **Shape:** The plan is not empty. Each line is printable ASCII, has no tab, and is not blank.
2. **Parse:** The driver reads each line exactly as written. The check blocks a repeated section, a line that the driver drops (such as `end`), a line that the driver changes, and a line under a negation.
3. **Destructive:** The check blocks the stems `relo`, `wr ... e`, `erase`, `crypto key zeroiz`, `forma`, and `req ... sy`, also after `do `, in any case.
4. **Future report:** The check blocks a negation that matches nothing in the running config.
5. **Convergence:** The lines of the future config of the plan and of the deterministic remediation are the same set.
6. **Fixed point:** After the plan, `hier_config` finds no remediation to do.
7. **Scaffolding:** Only a numbered permit-any entry can be scaffolding, such as `5 permit ip any any`. These conditions apply:
    - The pair has no child lines.
    - The removal names the entry exactly, or by its sequence number with no leading zero.
    - No running entry has that number.
    - The access list is at the top level.
    - The plan removes another entry of the access list.
    - The number is below every other entry.
8. **Forbidden:** No line matches a pattern that the caller supplies.
9. **Review:** The outcome lists every line that starts with the negation prefix.

Checks 4 to 6 use the plan without its scaffolding pairs. The outcome lists those pairs in `scaffolding`, so the approver sees them.

### Limits

- The token is an HMAC-SHA256 over the plan lines, the platform name, and the change fingerprint of the running config. The caller supplies the key, of 32 bytes or more. hco-core never stores or logs the key.
- Checks 10 to 14 of the specification need the run context. The review graph does them, not this function.
- The tree model resolves a short negation by its prefix: `no ip` removes every `ip` line. Outside a scaffolding pair, checks 5 and 6 block such a line only when the futures are different.
- The `CISCO_IOS` driver numbers an access list entry that has no sequence number. Check 2 therefore blocks that entry.
- Spec 08 permits the permit-any scaffolding only in an access list that is not tagged as critical for security. Devices have no tags in Phase 1, so the gate does not check this condition.
- A problem text quotes 200 characters or fewer.

## Freshness

`freshness_verdict()` decides if a stored running config is safe to use at a risk tier.

- `LOW` and `MEDIUM` use the snapshot when the fingerprints match.
- `HIGH` and `CRITICAL` always collect again.
- A missing fingerprint counts as a mismatch.
