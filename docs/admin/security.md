# Security

Read this page before you make the API available to other persons.

## The actor header

The API identifies a person by the `X-HCO-Actor` header. Nothing authenticates this header in Phase 1. Phase 3 replaces it with OIDC.

WARNING: Any client can send any name in `X-HCO-Actor`. One person can request a change and approve it with a different name. Do not expose the API on a shared network.

- A request that changes data needs the header. A request that only reads does not.
- A read request can show config lines, for example in `GET /api/v1/runs/{id}/diff`. These lines can hold sensitive values, such as password hashes or SNMP communities.

## Network exposure

- `hco-core serve api` listens on `127.0.0.1` by default.
- The development stack publishes the API port on `127.0.0.1` only.
- Do not use `--host 0.0.0.0` on a host that other persons can reach.

## Keys and tokens

- Make a new `HCO_VALIDATION_KEY` for each installation. See [Configuration](configuration.md#the-validation-key).
- Use a read-only Nautobot API token.
- Keep `HCO_NAUTOBOT_VERIFY_SSL=true` outside a lab.
- Do not commit a `.env` file.

## SSH host keys

The scrapli connection checks the host key of each device. Add each host key to the known hosts file of the user that runs hco-core. Do not turn off the check.

## What hco-core does not do

- No API route and no command writes to a device in Phase 1.
- The `hco-core` command writes to the database only with `db upgrade`. Other commands use the API.
- hco-core never reads a credential from the source of truth.
- An API error never shows a config text, a `metadata` value, or a database URL.
