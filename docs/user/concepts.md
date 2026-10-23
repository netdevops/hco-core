# Concepts

## A run

A run reviews one change on one device. You start a run, and hco-core does these steps:

1. It reads the intended config from the source of truth.
2. It reads the running config from the device.
3. It builds the remediation, the rollback, and the future config.
4. It checks that the remediation does what it says. A `no` line that matches nothing in the running config stops the run.
5. It sets the risk tier.
6. It sends a notification and opens an approval request.
7. It waits for the approvers.
8. It records the result.

## Run states

| State | Meaning |
|---|---|
| `DRAFT` | hco-core created the run. The review did not start. |
| `COLLECTING` | hco-core reads the configs. |
| `REMEDIATING` | hco-core builds the remediation. |
| `ASSESSING` | An assessment layer assesses the risk. Phase 1 has no assessment layer. |
| `EVALUATING_POLICY` | hco-core sets the risk tier. |
| `AWAITING_APPROVAL` | The run waits for the approvers. |
| `APPROVED` | The approvers approved the change. |
| `REJECTED` | An approver rejected the change. |
| `BLOCKED` | The run stopped. `error` gives the reason. |

The other states, such as `APPLYING` and `COMMITTED`, are for device writes. Phase 1 does not use them.

A run stops in `BLOCKED` in these conditions:

- The source of truth, the device, or the engine gave an error.
- The remediation has a `no` line that matches nothing in the running config. The error names each such line.
- Somebody cancelled the approval request while the run waited.

## Risk tiers

Each run has a risk tier. The tier sets the number of approvals that the change needs.

| Tier | Approvals |
|---|---|
| `LOW` | 1 |
| `MEDIUM` | 1 |
| `HIGH` | 2 |
| `CRITICAL` | 2 |

Phase 1 has no risk assessment. Each run therefore gets the tier `HIGH` and needs two approvals. The run field `unknowns` holds `no risk assessment`.

## Approvals

- The person who requested the change cannot approve it.
- Each person can decide one time on each request.
- One rejection rejects the request.
- When the request has enough approvals, the run is `APPROVED`.
- Any person can cancel a pending request, also the requester. A cancel only stops a change. A waiting run then stops in `BLOCKED`.
- Phase 1 does not check roles.

A request shows in the list only when its run is `AWAITING_APPROVAL`. A run that already stopped, for example in `BLOCKED`, takes no decision. You can still cancel its pending request.

## The diff of a run

The diff shows three sets of lines:

- The remediation: the lines that change the device to the intended config.
- The rollback: the lines that change the device back.
- The future config: the running config after the remediation.

Read the diff before you approve a run.
