# AGENTS.md

Instructions for AI coding agents that work in this repository. Keep this file
short. Put the reason next to each rule.

## Project

hco-core compares the running config of a network device with its intended config,
builds the remediation and the rollback, and sends the change to people for approval.
Network automation engineers use it and write adapters for its ports. It is in Phase
1: it reads from devices and records approvals, and it does not write to a device.

## Run and test

```bash
# start the app, then apply the database migrations
docker compose -f development/docker-compose.yml up -d --build --wait
docker compose -f development/docker-compose.yml exec hco-core-api hco-core db upgrade
# start the test database once; the database tests fail without it
docker run -d --name hco-core-test-pg -e POSTGRES_USER=hco \
  -e POSTGRES_PASSWORD=hco -e POSTGRES_DB=hco \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
# run the tests
HCO_DATABASE_URL=postgresql://hco:hco@127.0.0.1:55432/hco poetry run pytest -q --cov=hco_core --cov-report=term-missing --cov-fail-under=95
```

Run the full gate in `docs/developer/setup.md` before you open a pull request. CI runs
the same commands.

## Rules

- Make the smallest change that solves the problem. A small change is easy to review
  and easy to undo.
- Write a test for each change in behavior. The test is the proof that it works.
- Never commit a secret. Read secrets from environment variables. `.env.example` lists
  the names.
- Open a pull request for each change. Do not push to the default branch.
- Add a `towncrier` fragment in `changes/` for each change that a user sees. CI builds
  the fragments.
- Before an action that deletes data or costs money, state the risk and ask.

## Workflow

This repository uses `sdlc-crew`. Start a change with `/sdlc-crew "<what you want>"`.
The config is in `.sdlc-crew.yaml`.
