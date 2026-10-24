# Services

`hco_core.services` owns the writes of approvals, runs, and notifications. A surface, such as the API or the CLI, does not write these rows itself.

## ApprovalService

`hco_core.services.approval.ApprovalService` records approvals for each surface.

- `upsert_request()` opens one request for each change set. The requester must match `change_set.requested_by` exactly. If it does not, `upsert_request()` raises `RequesterMismatchError`.
- A second call to `upsert_request()` returns the same request. It never changes the requester.
- `decide()` records one decision. Each decision writes one `audit_event` row.
- One rejection rejects the request.
- The approvals close the request when they reach `required_count`. That count is never below the count of the risk tier in `hco_core.policy.tiers.APPROVALS_BY_TIER`.
- The tier count is one for `LOW` and `MEDIUM`, and two for `HIGH` and `CRITICAL`.
- When a decision closes the request, `decide()` calls the `resume` callback one time. Phase 1 does not check roles.
- `cancel()` closes a pending request with no decision. It writes one `audit_event` row and calls the `resume` callback one time with no decision.
- Any valid actor can cancel, also the requester, because a cancel only stops a change.
- A cancel also closes a pending request after its expiry, because Phase 1 has no expiry sweep.
- `decide()` locks the request row. Two decisions at the same time therefore close the request one time only.

The service raises these typed errors:

| Error | When |
|---|---|
| `RequesterMayNotApproveError` | The requester decides on their own change. |
| `DuplicateDecisionError` | The actor already decided on the request. |
| `InvalidActorError` | A name is empty, has more than 255 characters, has white space at an end, or has a control or invisible character. |
| `RequesterMismatchError` | The requester is not `change_set.requested_by`, or no change set has the id. |
| `ApprovalRequestNotFoundError` | No request has the id. |
| `ApprovalClosedError` | The request is not pending, or it expired. |
| `QuorumMismatchError` | A later `upsert_request()` asks for more approvals than the open request needs. |
| `NaiveExpiryError` | `expires_at` has no time zone. |

The requester check compares the NFKC, casefolded names. The database trigger also checks the exact names.

## RunRepository

`RunRepository` creates and updates change sets, runs, and config snapshots. Each state change writes one `audit_event` row.

## NotificationLedger

`NotificationLedger.deliver()` sends a message one time for each run, channel, and purpose.

- A later call updates the message.
- If a send failed, the ledger does not send it again.

## The database

`hco_core.db` holds the Postgres schema. The admin documentation gives the [tables and the database rules](../admin/database.md).

- `PlatformName` stores a platform by its name. It never stores the positional `Platform` value.
- Upgrade the schema with `hco_core.db.migrate.upgrade(CoreSettings())`.
- `hco_core.logging.configure_logging(settings)` sends structlog and standard-library log lines to stdout.
