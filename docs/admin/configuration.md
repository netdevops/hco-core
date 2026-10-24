# Configuration

hco-core reads its settings from environment variables with the prefix `HCO_`. It also reads a `.env` file in the current folder. `.env.example` shows each setting with its default.

## Settings

| Variable | Default | Use |
|---|---|---|
| `HCO_DATABASE_URL` | `postgresql+psycopg://hco:hco@localhost:5432/hco` | The Postgres database. |
| `HCO_LOG_LEVEL` | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO`, or `DEBUG`, in any case. |
| `HCO_LOG_FORMAT` | `json` | `json`, or `console` for a person to read. |
| `HCO_SOT` | `yaml` | The source of truth: `yaml` or `nautobot`. |
| `HCO_SOT_PATH` | `inventory.yaml` | The inventory file of the `yaml` source of truth. |
| `HCO_CONNECTION` | `mock` | The device connection: `mock` or `scrapli`. |
| `HCO_TRANSCRIPTS_DIR` | `transcripts` | The transcript folder of the `mock` connection. |
| `HCO_API_URL` | `http://localhost:8080` | The API that the `hco-core` command uses. |
| `HCO_ACTOR` | not set | The user name that the `hco-core` command sends. |
| `HCO_VALIDATION_KEY` | not set | The key that signs plans. The API needs it. |
| `HCO_NAUTOBOT_URL` | not set | The Nautobot URL. |
| `HCO_NAUTOBOT_TOKEN` | not set | A read-only Nautobot API token. |
| `HCO_NAUTOBOT_VERIFY_SSL` | `true` | The check of the Nautobot TLS certificate. |
| `HCO_NAUTOBOT_TIMEOUT` | `30` | The Nautobot request timeout, in seconds. |
| `HCO_NAUTOBOT_REDUNDANCY_TAG_PREFIX` | `hco-redundancy-` | The tag prefix of a redundancy group. |

Device credentials use other variables. See [Secrets](secrets.md).

- A `postgresql://` or `postgres://` URL gets the psycopg dialect, `postgresql+psycopg://`. Another scheme is an error.
- hco-core removes a user name and a password from `HCO_NAUTOBOT_URL`.
- `HCO_DATABASE_URL`, `HCO_VALIDATION_KEY`, and `HCO_NAUTOBOT_TOKEN` are secret. A printed settings object and a settings error do not show them.
- The `hco-core` commands that use the API read only `HCO_API_URL` and `HCO_ACTOR`. `db upgrade` and `serve api` read all the settings.

## The validation key

hco-core signs each plan that passes the validation gate. A device session refuses a plan whose signature does not verify. `HCO_VALIDATION_KEY` is the key of this signature.

- The key must have 32 bytes or more.
- hco-core never stores or logs the key.
- The API does not start without the key.

Make a key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

WARNING: The key in `development/.env` is public. Do not use it outside the development stack. hco-core refuses it with a connection other than `mock`.

## Logs

hco-core writes all log lines to stdout. The format is JSON, or text for a console when `HCO_LOG_FORMAT=console`.

These log lines help you find a problem:

| Event | Meaning |
|---|---|
| `request_failed` | An API request failed. `cause` and `reason` give the cause. |
| `run_failed` | The review of a run failed to start. |
| `device_skipped` | A device sync skipped a device. The line names the device and the reason. |
| `device_unreachable` | A device connection or command failed. |
| `nautobot_tls_verification_disabled` | `HCO_NAUTOBOT_VERIFY_SSL` is `false`. |
