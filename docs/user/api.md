# The REST API

The API is at `/api/v1`. `GET /openapi.json` gives the OpenAPI schema. The API serves no HTML page.

No route writes to a device. A run stops at `AWAITING_APPROVAL` until its approval request closes.

## Routes

| Route | Does |
|---|---|
| `GET /health` | Answers 200 when the database answers, and 503 when it does not. |
| `POST /api/v1/devices/sync` | Writes each device of the source of truth to the database. A second sync updates the rows. |
| `GET /api/v1/devices` | Lists the devices. |
| `GET /api/v1/devices/{id}` | Shows one device, by its row id or by its source of truth id. |
| `POST /api/v1/runs` | Creates a run on one device and answers 202. The review starts after the answer. |
| `GET /api/v1/runs` | Lists the runs, newest first. |
| `GET /api/v1/runs/{id}` | Shows one run, with its state, tiers, and errors. |
| `GET /api/v1/runs/{id}/diff` | Shows the remediation, the rollback, and the future config, as lines. |
| `GET /api/v1/approvals` | Lists the pending approval requests, newest first. |
| `GET /api/v1/approvals/{id}` | Shows one request, with its decisions and the state of its run. |
| `POST /api/v1/approvals/{id}/decide` | Records the decision of the actor. A decision that closes the request continues the run. |
| `POST /api/v1/approvals/{id}/cancel` | Closes the request with no decision. A waiting run stops in `BLOCKED`. |

## The actor header

A request that changes data needs the `X-HCO-Actor` header. Without it, the API answers 401.

- Send the name as UTF-8.
- The API stores the NFKC form of the name.
- The name must not be empty. It must not have white space at either end, and it must not hold a control character or an invisible character.

## Lists

A list answer holds `items` and `next_cursor`.

- Send `?cursor=<next_cursor>` to get the next page.
- Send `?limit=` to set the page size, from 1 to 500.

## Errors

An error is an RFC 9457 `application/problem+json` document. Its `type` does not change between releases, for example `urn:hco-core:problem:device-not-found`.

- An error never shows a config text, a `metadata` value, or a database URL.
- A run error shows a URL with no user information. It shows an absolute path by its base name only. It holds 500 characters or fewer.
- A 503 names the setting to check: `HCO_SOT_PATH`, `HCO_NAUTOBOT_URL` and `HCO_NAUTOBOT_TOKEN`, or `HCO_DATABASE_URL`.

The approval routes use these problem types:

| `type` suffix | Status | When |
|---|---|---|
| `requester-may-not-approve` | 403 | The requester decides on their own change. |
| `duplicate-decision` | 409 | The actor already decided on the request. |
| `approval-closed` | 409 | The request is approved, rejected, cancelled, or expired. |
| `run-not-awaiting-approval` | 409 | The request is pending, but its run does not wait. Nothing is written. |
| `run-not-parked` | 409 | The decision closed the request, but the run does not wait. The decision stays recorded. |
| `approval-pending` | 409 | The request is still pending. The run continues to wait. |
| `quorum-mismatch` | 409 | A request asks for more approvals than the open request needs. |
| `approval-not-found` | 404 | No request has the id. |

A 500 or a 503 after the request closes also keeps the decision recorded.

## Examples

Read the inventory, and show a device:

```bash
curl -s -X POST -H 'X-HCO-Actor: alice' localhost:8080/api/v1/devices/sync
curl -s 'localhost:8080/api/v1/devices?limit=10'
curl -s localhost:8080/api/v1/devices/r1
```

Start a run, and read its state and its diff:

```bash
curl -s -X POST -H 'X-HCO-Actor: alice' -H 'Content-Type: application/json' \
  -d '{"device_id": "r1"}' localhost:8080/api/v1/runs
curl -s localhost:8080/api/v1/runs/<run_id>
curl -s localhost:8080/api/v1/runs/<run_id>/diff
```

The run waits at `AWAITING_APPROVAL` with the tier `HIGH`. `unknowns` gives the reason:

```json
{"state": "AWAITING_APPROVAL", "risk_tier_final": "HIGH", "unknowns": ["no risk assessment"]}
```

`unknowns` is `null` when the run has no checkpoint yet. If the review fails to start, the run stops in `BLOCKED`. The error names the server log line `run_failed`.

Find the approval request of the run, and show it:

```bash
curl -s localhost:8080/api/v1/approvals
curl -s localhost:8080/api/v1/approvals/<request_id>
```

Alice requested the run, so the API refuses her decision with 403. Bob and Carol approve it:

```bash
curl -s -X POST -H 'X-HCO-Actor: alice' -H 'Content-Type: application/json' \
  -d '{"decision": "approve"}' localhost:8080/api/v1/approvals/<request_id>/decide
curl -s -X POST -H 'X-HCO-Actor: bob' -H 'Content-Type: application/json' \
  -d '{"decision": "approve", "comment": "checked the diff"}' \
  localhost:8080/api/v1/approvals/<request_id>/decide
curl -s -X POST -H 'X-HCO-Actor: carol' -H 'Content-Type: application/json' \
  -d '{"decision": "approve"}' localhost:8080/api/v1/approvals/<request_id>/decide
curl -s localhost:8080/api/v1/runs/<run_id>
```

After the second approval, the request is `approved` and the run is `APPROVED`:

```json
{"state": "approved", "required_count": 2, "run_state": "APPROVED"}
```

The run continues before the API answers. `run_state` in the answer is therefore the state after the decision: `AWAITING_APPROVAL`, `APPROVED`, or `REJECTED`.

To stop a change, cancel its request:

```bash
curl -s -X POST -H 'X-HCO-Actor: bob' -H 'Content-Type: application/json' \
  -d '{"comment": "wrong device"}' localhost:8080/api/v1/approvals/<request_id>/cancel
```

## Known gaps in Phase 1

- hco-core opens the approval request before the run waits. Until the run is `AWAITING_APPROVAL`, the list does not show the request. A decision or a cancel then gets `run-not-awaiting-approval`.
- A short gap occurs between the save of `AWAITING_APPROVAL` and the save of the checkpoint. A decision that closes the request in that gap gets `run-not-parked`. The run then continues to wait on the closed request.

A later phase closes these gaps.
