# Secrets

hco-core reads device credentials from environment variables. The source of truth never holds a credential. It gives only a key for each device.

## The credentials key of a device

| Source of truth | Key |
|---|---|
| YAML | The `secret` field of the device. The default is the device id. |
| Nautobot | `hco_secret_key` in the config context of the device. |

For Nautobot, these rules apply:

- `hco_secret_key` must be text of 1 to 128 characters.
- An error about `hco_secret_key` names the device and the key, never the value.
- If the config context has no `hco_secret_key`, the key is the device name in upper case. hco-core changes each character other than a letter, a digit, or `_` to `_`. If the device has no name, hco-core uses the device id.
- hco-core does not read Nautobot Secrets Groups. Phase 2 adds a `NautobotSecretsProvider` that reads them.

## The variables

For the key `<KEY>`, set these variables:

| Variable | Use |
|---|---|
| `HCO_SECRET_<KEY>_USERNAME` | The login user name. |
| `HCO_SECRET_<KEY>_PASSWORD` | The login password. |
| `HCO_SECRET_<KEY>_ENABLE` | Optional. The enable secret. |
| `HCO_SECRET_<KEY>_API_KEY` | Optional. An API key. |

To make `<KEY>`, write the key in upper case. Change each character that is not a letter or a digit to `_`.

| Key | Variable |
|---|---|
| `lab` | `HCO_SECRET_LAB_PASSWORD` |
| `edge-1` | `HCO_SECRET_EDGE_1_PASSWORD` |

- Two keys that differ only in these characters, such as `a-b` and `a_b`, read the same variables.
- An error names the variable, never its value.

For example, the mock connection reads the credentials of device `r1` from `HCO_SECRET_R1_USERNAME` and `HCO_SECRET_R1_PASSWORD`.

## The enable secret

scrapli sends the enable secret when the login user is not in privileged exec mode. Without `_ENABLE`, the login user needs privilege 15.

A blank `HCO_SECRET_<KEY>_ENABLE` is the same as no enable secret. A blank user name, password, or API key is an empty value.

A private key is not used in Phase 1.
