# Adapters

`hco_core.adapters` holds the built-in adapters. An adapter implements a port. See [Ports and design](ports.md).

## The registry

`hco_core.adapters.registry` selects an adapter by its name. It holds the built-in factories only. An unknown name gives an error that lists the known names.

| Port | Names |
|---|---|
| `SourceOfTruth` | `yaml`, `nautobot` |
| `SecretsProvider` | `env` |
| `ConnectionDriver` | `mock`, `scrapli` |
| `NotificationChannel` | `log` |

## Development adapters

These adapters need no external service. Use them for development and for tests.

| Adapter | Does |
|---|---|
| `YamlSourceOfTruth(path)` | Reads one inventory file on each call. |
| `EnvSecretsProvider()` | Reads credentials from environment variables. |
| `MockConnectionDriver(transcripts_dir, token_key=key)` | Replays transcript files. |
| `LogChannel()` | Writes each notification to the log. |

The admin documentation gives the [inventory format](../admin/source-of-truth.md#the-yaml-inventory), the [secret variables](../admin/secrets.md), and the [transcript layout](../admin/connections.md#the-mock-connection).

`MockConnectionDriver` rejects a device id that leaves `transcripts_dir`.

## NautobotSourceOfTruth

`NautobotSourceOfTruth` reads devices, intended configs, and topology from Nautobot. The admin documentation gives the [setup and the limits](../admin/source-of-truth.md#nautobot).

- Each POST is a GraphQL query. The adapter never sends a PUT, a PATCH, or a DELETE.
- The queries are constants with variables.
- `secret_ref()` returns an `env` reference. It never reads a secret from Nautobot.
- A device that the adapter cannot map is not in the page. `DevicePage.skipped` gives its name and the reason.
- A failed request raises `SourceOfTruthUnreachableError`, an `OSError`.
- `repr()` does not show the token.

## ScrapliConnectionDriver

`ScrapliConnectionDriver(token_key=key)` reads from a device over SSH with [scrapli](https://github.com/carlmontanari/scrapli). It uses the synchronous `Scrapli` driver under `asyncio.to_thread`, with the system transport. The admin documentation gives the [setup](../admin/connections.md#the-scrapli-connection).

- `fetch_running()` sends `show running-config`.
- `config_fingerprint()` sends the fingerprint command of the platform table.
- `collect_state()` refuses a command outside the allowlist with `CommandNotAllowedError`, before it sends a command.
- `dry_run()` reports no support.
- `apply()` verifies the validation token against the running config of the device, then always raises `DeviceWritesDisabledError`.
- `confirm()` and `rollback()` always raise `DeviceWritesDisabledError`.
- A connection error or a command error raises `DeviceUnreachableError`. The message never holds a credential.

The mock driver has the same behavior for `collect_state()`, `dry_run()`, `apply()`, `confirm()`, and `rollback()`.

## Write an adapter

1. Implement the methods of the port. You do not need a base class.
2. Return domain models from `hco_core.domain`.
3. Raise the typed errors of the port, for example `DeviceUnreachableError` or `SourceOfTruthUnreachableError`.
4. Keep credentials out of every error message, log line, and `repr()`.
5. For a `SourceOfTruth`, return an opaque `SecretRef`. Never return a credential.
6. For a `DeviceSession`, verify the validation token in `apply()` before any write.
