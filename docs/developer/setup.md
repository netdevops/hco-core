# Development setup

## Install

Install the package, the development tools, the scrapli extra, and MkDocs:

```bash
poetry install --all-extras --with docs
```

Without the scrapli extra, the scrapli tests skip.

## The test database

The database tests need Postgres. Start a test container on port 55432:

```bash
docker run -d --name hco-core-test-pg -e POSTGRES_USER=hco \
  -e POSTGRES_PASSWORD=hco -e POSTGRES_DB=hco \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
export HCO_DATABASE_URL=postgresql://hco:hco@127.0.0.1:55432/hco
```

If Postgres is not available, the database tests fail. They do not skip.

## The gate

Run the gate before you open a pull request. CI runs the same commands:

```bash
poetry run ruff format --check .
poetry run ruff check .
poetry run mypy hco_core
poetry run pyright
poetry run hco-core lint-interrupts hco_core
poetry run pytest --cov=hco_core
poetry run pytest -q tests/engine/test_validation.py --cov=hco_core.engine.validation --cov-branch --cov-fail-under=100
poetry check --lock
poetry run towncrier build --draft --version 0.0.0
poetry run mkdocs build --strict
```

- The validation gate needs 100% branch coverage.
- The `ci` workflow runs all the commands except the site build. The `docs` workflow builds the site.

## The documentation

The documentation uses MkDocs. Preview it on `http://127.0.0.1:8000`:

```bash
poetry run mkdocs serve
```

The gate builds the site with `--strict`, so a broken link fails the build.

The `docs` workflow publishes the site to https://netdevops.github.io/hco-core/ after each push to `develop`.

Write the documentation in ASD-STE100 Simplified Technical English.

## Changelog fragments

Add a changelog fragment in `changes/`. The rules are in `changes/README.md`.

- Name each fragment `<issue>.<type>`, for example `1.added`.
- Use one of these types: `added`, `changed`, `fixed`, `removed`, `security`, `dependencies`, `documentation`.
- Write one item on each line. Do not add a bullet marker.

Preview the render before you commit:

```bash
poetry run towncrier build --draft --version 0.0.0
```
