# Review graph

`hco_core.graphs.review` holds the LangGraph graph of one run. No node calls a model. Compile a graph with a checkpointer, and start it with `start_run()`.

## Collect, remediate, and the gate

1. `collect_running` reads the device of the run from the source of truth, with the id and the platform of the `device` row. It reads the intended config and the running config, and stores both snapshots.
2. `build_remediation` builds the remediation, the rollback, and the future config, with the plugins and the tag rules of `ReviewDeps`. It stores the future snapshot and puts the config-derived facts in `evidence["config_derived"]`.
3. The convergence gate sends a remediation with an unresolved negation to `blocked`. No node after the gate runs then. An unresolved negation is a `no` line that matches nothing in the running config. The run error names each such line.

These conditions also stop the run in `BLOCKED`, with the error in `remediation_run.error`:

- An error from the source of truth, the device, the engine, or a plugin.
- A device that is not the device of the run.
- A config with a NUL character.

The graph input is `run_id`. A run after `COLLECTING` does not start again. A node that runs again writes no second row.

- `build_collect_graph()` ends a run that passes the gate.
- `add_collect_nodes()` sends a run that passes the gate to a node that you name.

This example takes a run to the gate with the YAML source of truth and the mock driver. It reads `HCO_DATABASE_URL`. No service writes the `device` row yet, so the example adds the row directly.

```python
import asyncio
import tempfile
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.db import models
from hco_core.db.migrate import upgrade
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.db.types import new_id
from hco_core.graphs.review import ReviewDeps, build_collect_graph, start_run
from hco_core.services.audit import Actor
from hco_core.services.runs import RunRepository
from hco_core.settings import CoreSettings

folder = Path(tempfile.mkdtemp())
(folder / "inventory.yaml").write_text(
    "devices:\n"
    "  - {id: edge-1, name: edge-1, platform_name: CISCO_IOS,\n"
    "     mgmt_address: 10.0.0.1, intended_config: intended.txt}\n"
)
(folder / "intended.txt").write_text("hostname edge-1\nntp server 192.0.2.10\n")
(folder / "transcripts" / "edge-1").mkdir(parents=True)
(folder / "transcripts" / "edge-1" / "running.txt").write_text("hostname edge-1\n")

settings = CoreSettings()
upgrade(settings)


async def review_to_the_gate() -> str:
    engine = create_database_engine(settings)
    sessions = create_sessionmaker(engine)
    device_id = new_id()
    async with sessions.begin() as session:
        session.add(
            models.Device(
                id=device_id,
                sot_name="yaml",
                sot_device_id="edge-1",
                name="edge-1",
                platform_name="CISCO_IOS",
                mgmt_address="10.0.0.1",
            )
        )
    runs = RunRepository(sessions)
    alice = Actor("alice", "user", "cli")
    change_set_id = await runs.create_change_set("Add an NTP server", alice)
    run_id = await runs.create_run(change_set_id, device_id, "thread-1", alice)
    deps = ReviewDeps(
        sot=YamlSourceOfTruth(folder / "inventory.yaml"),
        secrets=EnvSecretsProvider(
            {"HCO_SECRET_EDGE_1_USERNAME": "admin", "HCO_SECRET_EDGE_1_PASSWORD": "x"}
        ),
        connection=MockConnectionDriver(folder / "transcripts", token_key=b"k" * 32),
        runs=runs,
    )
    graph = build_collect_graph(deps).compile(checkpointer=InMemorySaver())
    await start_run(graph, {"run_id": run_id}, "thread-1")
    state = (await runs.get_run(run_id)).state
    await engine.dispose()
    return state


assert asyncio.run(review_to_the_gate()) == "REMEDIATING"
```

## Policy and approval

`build_review_graph(deps, *, assessment=None)` adds the approval step after the gate.

- Its `deps` is an `ApprovalDeps`. It also holds the approval service, the notification ledger, and the channel names.
- Its input needs `run_id`, `change_set_id`, and `requested_by`. The first node checks the three keys before it writes anything.

The graph adds these nodes:

1. `evaluate_policy` sets the final risk tier. The tier is never below the floor. With no assessment, the tier is `HIGH`, and `unknowns` holds `no risk assessment`.
2. `notify_pending` sends one message on each channel.
3. `request_approval` opens the approval request and stops the run at its one `interrupt()`. Resume it with `Command(resume={"decision", "actor", "comment"})`.
4. `record_decision` reads the approval request from the database. The run is `APPROVED` or `REJECTED` only when the request closed that way and the resume payload names the same decision. In all other cases the run stops in `BLOCKED` with the reason.
5. `notify_decided` sends one more message, with the decisions of the request.

A run that left the review before a node writes, for example an expired run, keeps its state. No message goes out for it.

### The assessment hook

An assessment layer adds its nodes with the `assessment` hook.

- The hook returns the names of its first node and its last node.
- A hook node must catch its own errors. It must write each error into `errors` under its own node name.
- A later node cannot lower `risk_tier_floor` or `risk_tier_final`.
- An empty value or `None` keeps the tier. A name that is not a tier stops the run in `BLOCKED`.

This example stops a run for approval, records two approvals, and resumes the run from the `resume` callback of `ApprovalService`. It reads `HCO_DATABASE_URL`, and it adds the `device` row directly.

```python
import asyncio
import tempfile
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.notify.log import LogChannel
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.db import models
from hco_core.db.migrate import upgrade
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.db.types import new_id
from hco_core.domain.approval import ApprovalDecision, ApprovalRequest, Decision
from hco_core.graphs.review import ApprovalDeps, build_review_graph, start_run
from hco_core.services.approval import ApprovalService
from hco_core.services.audit import Actor
from hco_core.services.ledger import NotificationLedger
from hco_core.services.runs import RunRepository
from hco_core.settings import CoreSettings

folder = Path(tempfile.mkdtemp())
(folder / "inventory.yaml").write_text(
    "devices:\n"
    "  - {id: edge-1, name: edge-1, platform_name: CISCO_IOS,\n"
    "     mgmt_address: 10.0.0.1, intended_config: intended.txt}\n"
)
(folder / "intended.txt").write_text("hostname edge-1\nntp server 192.0.2.10\n")
(folder / "transcripts" / "edge-1").mkdir(parents=True)
(folder / "transcripts" / "edge-1" / "running.txt").write_text("hostname edge-1\n")

settings = CoreSettings()
upgrade(settings)
config = {"configurable": {"thread_id": "thread-1"}}


async def review_to_approval() -> str:
    engine = create_database_engine(settings)
    sessions = create_sessionmaker(engine)
    device_id = new_id()
    async with sessions.begin() as session:
        session.add(
            models.Device(
                id=device_id,
                sot_name="yaml",
                sot_device_id="edge-1",
                name="edge-1",
                platform_name="CISCO_IOS",
                mgmt_address="10.0.0.1",
            )
        )

    async def resume(request: ApprovalRequest, decision: ApprovalDecision) -> None:
        payload = {
            "decision": decision.decision.value,
            "actor": decision.actor,
            "comment": decision.comment,
        }
        await graph.ainvoke(Command(resume=payload), config, durability="sync")

    runs = RunRepository(sessions)
    approvals = ApprovalService(sessions, resume=resume)
    deps = ApprovalDeps(
        sot=YamlSourceOfTruth(folder / "inventory.yaml"),
        secrets=EnvSecretsProvider(
            {"HCO_SECRET_EDGE_1_USERNAME": "admin", "HCO_SECRET_EDGE_1_PASSWORD": "x"}
        ),
        connection=MockConnectionDriver(folder / "transcripts", token_key=b"k" * 32),
        runs=runs,
        approvals=approvals,
        ledger=NotificationLedger(sessions, {"log": LogChannel()}),
    )
    graph = build_review_graph(deps).compile(checkpointer=InMemorySaver())
    alice = Actor("alice", "user", "cli")
    change_set_id = await runs.create_change_set("Add an NTP server", alice)
    run_id = await runs.create_run(change_set_id, device_id, "thread-1", alice)
    state = {"run_id": run_id, "change_set_id": change_set_id, "requested_by": "alice"}
    await start_run(graph, state, "thread-1")
    (pause,) = (await graph.aget_state(config)).interrupts
    request_id = pause.value["request_id"]
    await approvals.decide(request_id, "bob", Decision.APPROVE, "cli")
    await approvals.decide(request_id, "carol", Decision.APPROVE, "cli")
    final = (await runs.get_run(run_id)).state
    await engine.dispose()
    return final


assert asyncio.run(review_to_approval()) == "APPROVED"
```

## Durable checkpoints and resume

A run can wait days for a person. `open_checkpointer(settings)` gives an `AsyncPostgresSaver` on its own connection, so a waiting run continues after a restart. Compile the review graph with this saver in production. Use `InMemorySaver` only in tests.

- The saver keeps its four tables (`hco_core.db.models.CHECKPOINT_TABLES`) in the schema `hco`. The Alembic chain does not own them.
- Run `upgrade(settings)` first, because it makes the schema `hco`. Then run `setup_checkpointer(settings)` one time, as the deploy step. The setup is safe to run again.
- `open_checkpointer` makes no table.
- A URL that does not parse gives `InvalidDatabaseUrlError`. The error does not show the URL.

`RunResumer(runs, graph, settings, approvals=...).resume(run_id, decision)` is the only way to continue a waiting run.

- It reads the thread of the run, opens a new saver, and compiles a new graph.
- It resumes with `Command(resume=...)` and `durability="sync"`.
- A thread with no pending interrupt gives `NotParkedError`, and nothing is written.
- It refuses a run whose approval request is still pending with `RequestPendingError`. The run continues to wait.
- The payload never approves. `record_decision` reads the closed request from the database.

`review_with_resume(settings, sessions, review)` builds the review dependencies with an approval service whose `resume` callback is `RunResumer.resume_closed`. Each closed request resumes its run one time. After a cancel, a waiting run stops in `BLOCKED`, and a run that does not wait keeps its state.

```python
import asyncio

from hco_core.db.migrate import upgrade
from hco_core.graphs.checkpointer import open_checkpointer, setup_checkpointer
from hco_core.settings import CoreSettings

settings = CoreSettings()
upgrade(settings)
asyncio.run(setup_checkpointer(settings))


async def latest_checkpoint() -> object:
    async with open_checkpointer(settings) as saver:
        return await saver.aget_tuple({"configurable": {"thread_id": "no-run"}})


assert asyncio.run(latest_checkpoint()) is None
```

## One interrupt in each node

A resumed node runs again from the start. A node with a second `interrupt()`, or with an `interrupt()` that only some runs reach, asks the person a different question on each run. The `interrupts` lint rule stops this in CI:

```bash
poetry run hco-core lint-interrupts hco_core
```

The command prints each violation as `path:line: function: reason`, and it exits with code 1. It fails on these:

- A second `interrupt()` in one function.
- An `interrupt()` inside `if`, `for`, `while`, `try`, `with`, `match`, `assert`, a conditional expression, a boolean operator, a comprehension, or a lambda.
- An `interrupt()` outside a function body, for example in a decorator or a default argument.
- A `return`, `raise`, `break`, or `continue` inside one of these constructs above the `interrupt()`.

The rule permits calls above the `interrupt()`, so make each one idempotent. The rule does not follow a local alias such as `ask = interrupt`, or `getattr`.
