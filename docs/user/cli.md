# The command line

The `hco-core` command is a client of the REST API. It sends requests to `HCO_API_URL`. It never writes to a device.

The command approves only through `POST /api/v1/approvals/{id}/decide`.

## Your user name

A command that changes data sends your user name in the `X-HCO-Actor` header, as UTF-8. Give the name in one of these two ways:

- Use the `--as <name>` option.
- Set `HCO_ACTOR` in your shell.

When you give `--as`, the command does not use `HCO_ACTOR`, also when `--as` is empty.

A name must have 255 characters or fewer. It must not have white space at either end. It must not hold a control character or an invisible character. A wrong name stops the command with exit code 2.

## Commands

| Command | Does |
|---|---|
| `run create --device <id> [--title] [--no-wait] [--timeout]` | Starts a run and waits until its review stops. |
| `run list [--state]` | Shows the runs. |
| `run show <id>` | Shows one run. |
| `run diff <id>` | Shows the remediation and the rollback of a run. |
| `approval list` | Shows the pending approval requests. |
| `approve <id> [--comment]` | Approves an approval request. |
| `reject <id> --comment` | Rejects an approval request. The comment is necessary. |
| `cancel <id> [--comment]` | Closes an approval request with no decision. |
| `device list` | Shows the devices. |
| `device sync` | Reads the devices from the source of truth. |

The commands `db upgrade` and `serve api` are for the admin. See the [admin documentation](../admin/install.md).

- `approve`, `reject`, and `cancel` take the id of the approval request, not the id of the run. `approval list` shows the run of each request.
- A list shows a table. Use `--limit` and `--cursor` to move through the pages.
- `--json` prints the answer of the API as JSON. `run diff --json` also gives the future config.

## Example

Alice requests a run. Bob and Carol approve it.

```bash
hco-core device sync --as alice
hco-core run create --device r1 --as alice
hco-core approval list
hco-core approve <request_id> --as bob
hco-core approve <request_id> --as carol
hco-core run show <run_id>
```

After the second approval, the run is `APPROVED`.

## Exit codes of `run create`

| Code | Meaning |
|---|---|
| 0 | The run is `AWAITING_APPROVAL` or `APPROVED`. |
| 1 | The run stopped in a different state, such as `BLOCKED`. |
| 2 | An option or the user name is not correct. |
| 3 | The wait timed out. |

`--timeout` takes 0 to 3600 seconds. It also limits each request.

## Errors

- An error is one sentence on stderr. The exit code is 1.
- With `--json`, stdout also gets the problem document of the API.
- A message never shows a password from `HCO_API_URL` or `HCO_DATABASE_URL`.
- The command removes control characters from each text of the API before it prints the text.

If the API does not answer a change in time, the API can still do the change. Run `run list` or `approval list` before you try again.
