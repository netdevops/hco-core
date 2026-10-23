"""Fixtures of the REST API tests: an app on the test database, YAML, and mock."""

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from hco_core.adapters.conn.mock import MockConnectionDriver
from hco_core.adapters.secrets.env import EnvSecretsProvider
from hco_core.adapters.sot.yaml_file import YamlSourceOfTruth
from hco_core.api.app import create_app
from hco_core.api.deps import ApiDeps
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.graphs.checkpointer import setup_checkpointer
from hco_core.graphs.review import ApprovalDeps
from hco_core.ports import SourceOfTruth
from hco_core.services.ledger import NotificationLedger
from hco_core.services.resume import review_with_resume
from hco_core.services.runs import RunRepository
from hco_core.settings import CoreSettings
from tests.graphs.test_review import (
    ENVIRON,
    INTENDED,
    INVENTORY,
    RUNNING,
    RecordingChannel,
)

ACTOR = "alice"
ACTOR_HEADER = "X-HCO-Actor"


@dataclass
class Api:
    """Send requests to the app, and give the services under it."""

    runner: asyncio.Runner
    app: FastAPI
    deps: ApiDeps
    root: Path

    def call(
        self,
        method: str,
        path: str,
        *,
        actor: str | bytes | None = None,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        """Send one request, with the actor header as UTF-8 when an actor is given."""
        value = actor.encode() if isinstance(actor, str) else actor
        headers = {} if value is None else {ACTOR_HEADER.encode(): value}
        return self.runner.run(_send(self.app, method, path, headers, json))

    def call_together(
        self, method: str, calls: Sequence[tuple[str, str, Mapping[str, object]]]
    ) -> list[httpx.Response]:
        """Send each `(path, actor, json)` request at the same time."""

        async def send_all() -> list[httpx.Response]:
            return await asyncio.gather(
                *(
                    _send(
                        self.app,
                        method,
                        path,
                        {ACTOR_HEADER.encode(): actor.encode()},
                        json,
                    )
                    for path, actor, json in calls
                )
            )

        return self.runner.run(send_all())


async def _send(
    app: FastAPI,
    method: str,
    path: str,
    headers: Mapping[bytes, bytes],
    json: Mapping[str, object] | None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://hco.test"
    ) as client:
        return await client.request(method, path, headers=headers, json=json)


def write_inventory(root: Path, inventory: str = INVENTORY) -> None:
    """Write the inventory, the intended config of r1, and its running config."""
    (root / "inventory.yaml").write_text(inventory, encoding="utf8")
    (root / "intended.txt").write_text(INTENDED, encoding="utf8")
    (root / "transcripts" / "r1").mkdir(parents=True, exist_ok=True)
    (root / "transcripts" / "r1" / "running.txt").write_text(RUNNING, "utf8")


@pytest.fixture
def settings(database_url: str) -> CoreSettings:
    """Give settings on the test database."""
    return CoreSettings(database_url=SecretStr(database_url))


@pytest.fixture
def sot(tmp_path: Path) -> SourceOfTruth:
    """Give the YAML source of truth of the app. A test module can override it."""
    return YamlSourceOfTruth(tmp_path / "inventory.yaml")


@pytest.fixture
def api(
    runner: asyncio.Runner,
    settings: CoreSettings,
    clean_tables: None,
    tmp_path: Path,
    sot: SourceOfTruth,
) -> Iterator[Api]:
    """Give the app on the test database, with the YAML inventory and mock devices.

    Yields:
        The app and its dependencies. The engine is disposed of after the test.

    """
    del clean_tables
    write_inventory(tmp_path)
    runner.run(setup_checkpointer(settings))
    engine = create_database_engine(settings)
    sessions = create_sessionmaker(engine)
    review = review_with_resume(
        settings,
        sessions,
        lambda approvals: ApprovalDeps(
            sot=sot,
            secrets=EnvSecretsProvider(ENVIRON),
            connection=MockConnectionDriver(
                tmp_path / "transcripts", token_key=b"k" * 32
            ),
            runs=RunRepository(sessions),
            approvals=approvals,
            ledger=NotificationLedger(sessions, {"test": RecordingChannel()}),
            channels=("test",),
        ),
    )
    deps = ApiDeps(engine=engine, sessions=sessions, review=review)
    yield Api(runner, create_app(settings, deps), deps, tmp_path)
    runner.run(engine.dispose())
