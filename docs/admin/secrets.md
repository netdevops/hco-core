# Secrets

hco-core reads device credentials from environment variables. The source of truth never holds a credential. It gives only a key for each device.

## The credentials key of a device

| Source of truth | Key |
|---|---|
| YAML | The `secret` field of the device. The default is the device id. |
| Nautobot | `hco_secret_key` in the config context of the device. The default is the device name. |

`hco_secret_key` must be text of 1 to 128 characters.

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
- A blank value is the same as no value.
- An error names the variable, never its value.

## The enable secret

scrapli sends the enable secret when the login user is not in privileged exec mode. Without `_ENABLE`, the login user needs privilege 15.

A private key is not used in Phase 1.
