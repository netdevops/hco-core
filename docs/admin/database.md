# Database

hco-core keeps its data in Postgres 16, in the schema `hco`. Set the database with `HCO_DATABASE_URL`.

## Upgrade the schema

Run this command before the first start, and after each upgrade of hco-core:

```bash
hco-core db upgrade
```

The command does two steps:

1. It upgrades the schema with Alembic. The Alembic version table is `hco.alembic_version`.
2. It makes the checkpointer tables. This step is safe to do again.

## Tables

The Alembic schema has eight tables:

| Table | Holds |
|---|---|
| `device` | The devices from the source of truth. |
| `config_snapshot` | The running, intended, and future configs of a run. |
| `change_set` | The requests for a change. |
| `remediation_run` | The runs, with their states and errors. |
| `approval_request` | The approval requests. |
| `approval_decision` | The decisions on the approval requests. |
| `audit_event` | One row for each state change and each decision. |
| `notification_delivery` | One row for each notification that hco-core sent. |

- Each table has a ULID key, `created_at`, `updated_at`, and `tenant_id`. `tenant_id` stays empty in version 1.
- The database stores a platform by its name, such as `CISCO_IOS`.

## Rules in the database

The database refuses some changes itself, also from a direct SQL statement:

- A trigger refuses a decision by the person who requested the change, also on a direct `INSERT` or `UPDATE`.
- A trigger refuses a change of the requester of an approval request, also through `INSERT ... ON CONFLICT DO UPDATE`.
- A requester or an actor must not be empty. It must have no white space at either end, such as a space, a tab, a new line, or a non-breaking space.
- The trigger compares the names with case sensitivity.
- `device.platform_name` must be in upper case and must not be a number.
- One person can make one decision on each approval request.
- hco-core sends one notification for each run, channel, and purpose.

## The checkpointer tables

A run can wait days for an approval. The checkpointer keeps the state of a waiting run in Postgres, so the run continues after a restart.

- The checkpointer has four tables in the schema `hco`. Alembic does not own them.
- `hco-core db upgrade` makes them. Run the upgrade first, because the upgrade makes the schema `hco`.
- At runtime, the database role needs only these permissions:
  - `USAGE` on the schema `hco`.
  - `SELECT`, `INSERT`, `UPDATE`, and `DELETE` on the tables.
- The checkpointer adds `-c search_path=hco` to the `options` of `HCO_DATABASE_URL`. It keeps your other options, such as `statement_timeout`.

## Known gaps in Phase 1

- If the process stops before a run waits for approval, the run stays in its last state.
- If the process stops after a decision closes the request and before the run continues, the run stays at `AWAITING_APPROVAL`.
- Phase 1 has no sweep of expired approval requests.

Phase 1 does not continue these runs. A later phase adds a reconciler for them.
