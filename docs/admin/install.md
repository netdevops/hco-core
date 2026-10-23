# Install

## Requirements

- Python 3.12 to 3.14.
- Postgres 16.
- For the scrapli connection: the `ssh` command on the host that runs hco-core.

Read `pyproject.toml` for the runtime dependencies. The list grows in each Phase 1 release.

## Install the package

```bash
pip install hco-core
```

Install the scrapli extra to read from real devices over SSH:

```bash
pip install 'hco-core[scrapli]'
```

The extra pins scrapli to `>=2025.1.30,<2025.8`. The releases 2.0.0a1 to 2.0.0a5, and the releases from 2025.8.16b1 on, have a different API.

## Prepare a new installation

Do these steps in this order:

1. Make a validation key. See [Configuration](configuration.md#the-validation-key).
2. Set `HCO_DATABASE_URL` and the other settings. See [Configuration](configuration.md).
3. Upgrade the database schema:

   ```bash
   hco-core db upgrade
   ```

4. Start the API:

   ```bash
   hco-core serve api --host 127.0.0.1 --port 8080
   ```

5. Read the inventory into the database:

   ```bash
   hco-core device sync --as <your name>
   ```

You can also start the API with uvicorn:

```bash
uvicorn --factory hco_core.api.app:app_from_env --port 8080
```

## The development stack

The folder `development/` holds a Docker Compose stack. It starts Postgres and the API. Use it to try hco-core, not in production.

```bash
docker compose -f development/docker-compose.yml up -d --build --wait
docker compose -f development/docker-compose.yml exec hco-core-api hco-core db upgrade
```

- The API listens on `http://127.0.0.1:8080`. Compose publishes the port on the loopback address only.
- The API reads `tests/fixtures/inventory.yaml` with the mock connection.
- The settings come from `development/.env`.
- If port 8080 is in use, set `HCO_API_PORT` in `development/.env`. Then set `HCO_API_URL` in your shell.

WARNING: The values in `development/.env` are public. Make a new validation key before you use hco-core outside this stack. hco-core refuses the public key with a connection other than `mock`.

Stop the stack and delete its data:

```bash
docker compose -f development/docker-compose.yml down -v
```
