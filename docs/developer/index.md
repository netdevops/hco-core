# Developer documentation

This part is for the person who changes hco-core or writes an adapter for it.

1. [Development setup](setup.md): install, the test database, the gate, and changelog fragments.
2. [Ports and design](ports.md): the nine ports, the AI contract, and the design rules.
3. [Engine](engine.md): remediation, config-derived facts, the validation gate, and freshness.
4. [Adapters](adapters.md): the adapter registry and the built-in adapters.
5. [Services](services.md): approvals, runs, and notifications.
6. [Review graph](review-graph.md): the LangGraph graph of a run, checkpoints, and resume.

## Package layout

| Package | Holds |
|---|---|
| `hco_core.domain` | The domain models. |
| `hco_core.ports` | The nine ports, as `typing.Protocol` classes. |
| `hco_core.engine` | The only package that imports `hier_config`. |
| `hco_core.adapters` | The built-in adapters and the registry. |
| `hco_core.db` | The SQLAlchemy models, the migrations, and the sessions. |
| `hco_core.services` | The services that write a run, an approval, and a notification. |
| `hco_core.policy` | The table of approvals for each risk tier. |
| `hco_core.graphs` | The review graph and the checkpointer. |
| `hco_core.api` | The FastAPI app. |
| `hco_core.cli` | The `hco-core` command. |
| `hco_core.lint` | The `interrupts` lint rule. |
