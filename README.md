# hco-core

**Ports, domain models, and a review service for network configuration orchestration on
[`hier_config`](https://github.com/netdevops/hier_config).**

hco-core compares the running config of a network device with its intended config. It builds the remediation and the rollback, and it checks that the remediation does what it says. Then it sends the change to people for approval.

hco-core has these parts:

- **Ports:** Nine port contracts, as `typing.Protocol` classes. They cover a source of truth, a device connection, a secrets store, a model, a risk analyser, and other services. Anyone can write an adapter for them.
- **Engine:** The remediation, the config facts of a change, and the validation gate that signs a safe plan.
- **Review service:** A REST API, a command line, and a Postgres database. They record each run and each approval.
- **Adapters:** A YAML file and Nautobot as the source of truth, and transcript files and scrapli for device reads.

hco-core is in Phase 1. It reads from devices and records approvals. It does not write to a device.

```bash
pip install hco-core
```

## Quick start

This quick start takes a change on the YAML test inventory to `APPROVED`. It uses the REST API and the `hco-core` command.

You need Docker with Compose, and Poetry. Run the commands in the repository folder:

```bash
docker compose -f development/docker-compose.yml up -d --build --wait
docker compose -f development/docker-compose.yml exec hco-core-api hco-core db upgrade
poetry install
poetry run hco-core device sync --as alice
poetry run hco-core run create --device r1 --as alice
poetry run hco-core approval list
poetry run hco-core approve <request_id> --as bob
poetry run hco-core approve <request_id> --as carol
poetry run hco-core run show <run_id>
```

- The stack starts Postgres and the API on `http://127.0.0.1:8080`. The API reads `tests/fixtures/inventory.yaml` with the mock connection.
- `run create` waits until the run is `AWAITING_APPROVAL`. Then it shows the run id. `approval list` shows the request id of the run.
- With no risk assessment, the risk tier is `HIGH`. Two persons who did not request the change must approve it. After the second approval, the run is `APPROVED`.
- If port 8080 is in use, set `HCO_API_PORT` in `development/.env`. Then set `HCO_API_URL` in your shell.

WARNING: The values in `development/.env` are public. Make a new `HCO_VALIDATION_KEY` before you use hco-core outside this stack.

Stop the stack and delete its data:

```bash
docker compose -f development/docker-compose.yml down -v
```

## Documentation

The documentation is in [`docs/`](https://github.com/netdevops/hco-core/tree/develop/docs):

- [Admin documentation](https://github.com/netdevops/hco-core/blob/develop/docs/admin/index.md): install, configuration, database, source of truth, secrets, device connections, and security.
- [User documentation](https://github.com/netdevops/hco-core/blob/develop/docs/user/index.md): runs, approvals, the command line, and the REST API.
- [Developer documentation](https://github.com/netdevops/hco-core/blob/develop/docs/developer/index.md): development setup, ports, engine, adapters, services, and the review graph.

Preview the documentation site:

```bash
poetry install --with docs
poetry run mkdocs serve
```

## Licence

Apache-2.0. See [`LICENSE`](https://github.com/netdevops/hco-core/blob/develop/LICENSE) and [`NOTICE`](https://github.com/netdevops/hco-core/blob/develop/NOTICE).
