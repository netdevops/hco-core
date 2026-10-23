# hco-core documentation

The documentation has three parts. Each part is for a different reader.

| Part | Reader | Topics |
|---|---|---|
| [Admin documentation](admin/index.md) | The person who installs and operates hco-core | Install, settings, database, source of truth, secrets, device connections, security |
| [User documentation](user/index.md) | The person who requests and approves changes | Runs, risk tiers, approvals, the `hco-core` command, the REST API |
| [Developer documentation](developer/index.md) | The person who changes hco-core or writes an adapter | Development setup, ports, engine, adapters, services, review graph |

## Words in this documentation

- **Run:** One review of one change on one device.
- **Change set:** The request for a change. A run belongs to one change set.
- **Remediation:** The configuration lines that change the running config into the intended config.
- **Rollback:** The configuration lines that change the device back.
- **Future config:** The running config after the remediation.
- **Approval request:** The request for approval of a run. The approvers decide on it.
- **Actor:** The person who sends a request to hco-core.
- **Source of truth:** The system that holds the inventory and the intended config, for example a YAML file or Nautobot.
- **Phase:** A stage of the project plan. Phase 1 reads devices and records approvals. Phase 4 adds device writes.
