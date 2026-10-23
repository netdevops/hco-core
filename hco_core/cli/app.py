"""The `hco-core` command.

The run, approval, and device commands are clients of the REST API. They
approve only through `POST /api/v1/approvals/{id}/decide`, and they never write
to the database or to a device. Only `db upgrade` opens the database.
"""

import asyncio
import json
import math
import time
import unicodedata
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, NoReturn
from urllib.parse import quote

import httpx
import typer
import uvicorn
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings

from hco_core.adapters.errors import AdapterConfigError
from hco_core.api.app import create_app
from hco_core.api.deps import (
    MAX_PAGE,
    DevelopmentKeyError,
    MissingValidationKeyError,
    ShortValidationKeyError,
    build_deps,
    refuse_development_key,
)
from hco_core.api.errors import failure_reason
from hco_core.api.routers.approvals import ApprovalDetail, ApprovalPage
from hco_core.api.routers.devices import DevicePage, SyncResult
from hco_core.api.routers.runs import RunCreated, RunDetail, RunDiff, RunPage
from hco_core.cli.client import TIMEOUT_SECONDS, ApiClient, ApiError, printable
from hco_core.db import migrate
from hco_core.domain.approval import Decision, RunState
from hco_core.graphs.checkpointer import setup_checkpointer
from hco_core.lint import interrupts
from hco_core.logging import configure_logging
from hco_core.services.approval import is_valid_name
from hco_core.settings import ClientSettings, CoreSettings

POLL_SECONDS: Final = 0.5
WORKING_STATES: Final = frozenset(
    {
        RunState.DRAFT,
        RunState.COLLECTING,
        RunState.REMEDIATING,
        RunState.ASSESSING,
        RunState.EVALUATING_POLICY,
    }
)
"""The states of a run whose review graph has not stopped yet."""
LABEL_WIDTH: Final = 16
MAX_WAIT_SECONDS: Final = 3600.0
MIN_REQUEST_SECONDS: Final = 2.0
"""The shortest time that one request of `run create` waits for the API."""
PARKED_STATES: Final = frozenset({RunState.AWAITING_APPROVAL, RunState.APPROVED})
"""The states in which `run create` exits 0."""
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_STILL_WORKING: Final = 3

RUN_CREATE_HELP: Final = (
    "Start a review of the device. The run parks for approval, or it ends BLOCKED."
    "\n\nThe exit code is 0 when the run is AWAITING_APPROVAL or APPROVED, 1 when"
    " it ends in a different state or on an error, 2 for a bad option or user"
    " name, and 3 when the wait times out."
)
"""The help of `run create`. The docstring also holds the part for developers."""

app = typer.Typer(
    name="hco-core",
    help=(
        "Review configuration changes and approve them through the hco-core API."
        "\n\nThe commands send requests to the API at HCO_API_URL. Your user name"
        " comes from --as or from HCO_ACTOR. The exit code is 0 on success, 1 on"
        " an error, and 2 for a bad option or user name. run create also exits 3"
        " when its wait times out."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
run_app = typer.Typer(
    help="Create runs, and show their state and diff.", no_args_is_help=True
)
approval_app = typer.Typer(
    help="Show the pending approval requests.", no_args_is_help=True
)
device_app = typer.Typer(
    help="Read the source of truth, and show the devices.", no_args_is_help=True
)
db_app = typer.Typer(help="Upgrade the database.", no_args_is_help=True)
serve_app = typer.Typer(help="Start a server.", no_args_is_help=True)
app.add_typer(run_app, name="run")
app.add_typer(approval_app, name="approval")
app.add_typer(device_app, name="device")
app.add_typer(db_app, name="db")
app.add_typer(serve_app, name="serve")

ActorOption = Annotated[
    str | None,
    typer.Option(
        "--as",
        help="Your user name. The API gets it in X-HCO-Actor. The default is HCO_ACTOR.",
    ),
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Print the answer of the API as JSON.")
]
LimitOption = Annotated[
    int | None,
    typer.Option(min=1, max=MAX_PAGE, help="The maximum number of items to show."),
]
CursorOption = Annotated[
    str | None,
    typer.Option(help="Show the next page. Use the cursor from the last page."),
]


@run_app.command("create", help=RUN_CREATE_HELP)
def run_create(  # ruff: ignore[too-many-arguments]
    ctx: typer.Context,
    *,
    device: Annotated[
        str,
        typer.Option(
            help="The device id in the source of truth, or the device row id."
        ),
    ],
    actor: ActorOption = None,
    title: Annotated[str | None, typer.Option(help="A title for the change.")] = None,
    wait: Annotated[
        bool, typer.Option(help="Wait until the review of the run stops.")
    ] = True,
    timeout: Annotated[
        float,
        typer.Option(
            min=0,
            max=MAX_WAIT_SECONDS,
            help="The maximum number of seconds to wait, also for each request.",
        ),
    ] = 30.0,
    as_json: JsonOption = False,
) -> None:
    """Start a review of the device. The run parks for approval, or it ends BLOCKED.

    The exit code is 0 when the run is AWAITING_APPROVAL or APPROVED, 1 when it
    ends in a different state or on an error, 2 for a bad option or user
    name, and 3 when the wait times out.

    Raises:
        typer.Exit: With exit code 1 or 3, when the run is not parked.

    """
    if math.isnan(timeout):
        _fail("--timeout must be a number of seconds", EXIT_USAGE)
    client = _client(ctx, actor)
    body = {"device_id": device} | ({"title": title} if title else {})
    deadline = time.monotonic() + timeout
    with _reported(as_json=as_json):
        created = client.post("/runs", RunCreated, body, _request_timeout(deadline))
        if not wait:
            _show(
                created,
                f"Run {created.run_id} is {created.state}. "
                f"Show it with: hco-core run show {created.run_id}",
                as_json=as_json,
            )
            return
        run = _wait(client, created.run_id, deadline)
    _show(run, _outcome(run, timeout), as_json=as_json)
    if run.state in WORKING_STATES:
        raise typer.Exit(EXIT_STILL_WORKING)
    if run.state not in PARKED_STATES:
        raise typer.Exit(EXIT_FAILED)


@run_app.command("list")
def run_list(
    ctx: typer.Context,
    *,
    state: Annotated[
        RunState | None, typer.Option(help="Show only the runs in this state.")
    ] = None,
    limit: LimitOption = None,
    cursor: CursorOption = None,
    as_json: JsonOption = False,
) -> None:
    """Show the runs, newest first."""
    params = _params(state=state, limit=limit, cursor=cursor)
    with _reported(as_json=as_json):
        page = _client(ctx).get("/runs", RunPage, params)
    if as_json or not page.items:
        _show(page, "No runs.", as_json=as_json)
        return
    _table(
        ("RUN", "DEVICE", "STATE", "TIER", "REQUESTED BY", "CREATED"),
        [
            (
                run.id,
                run.device_name,
                run.state,
                run.risk_tier_final or "-",
                run.requested_by,
                _time(run.created_at),
            )
            for run in page.items
        ],
    )
    _next_page("runs", page.next_cursor)


@run_app.command("show")
def run_show(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="The run id.")],
    *,
    as_json: JsonOption = False,
) -> None:
    """Show the state, the risk tier, the unknowns, and the errors of a run."""
    with _reported(as_json=as_json):
        run = _client(ctx).get(f"/runs/{quote(run_id, safe='')}", RunDetail)
    if as_json:
        _show(run, "", as_json=True)
        return
    unknowns = "not known yet" if run.unknowns is None else ", ".join(run.unknowns)
    _labels(
        ("Run", run.id),
        ("Device", f"{run.device_name} ({run.sot_device_id})"),
        ("State", run.state),
        ("Requested by", run.requested_by),
        (
            "Risk tier",
            f"{run.risk_tier_final or 'none'} (floor: {run.risk_tier_floor or 'none'})",
        ),
        ("Unknowns", unknowns or "none"),
        ("Plan source", run.plan_source),
        ("Fingerprint", run.change_fingerprint or "none"),
        ("Created", _time(run.created_at)),
        ("Updated", _time(run.updated_at)),
        ("Errors", "" if run.errors else "none"),
    )
    for error in run.errors:
        typer.echo(f"  - {printable(error)}")


@run_app.command("diff")
def run_diff(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="The run id.")],
    *,
    as_json: JsonOption = False,
) -> None:
    """Show the remediation and the rollback of a run. Use --json for the future config."""
    with _reported(as_json=as_json):
        diff = _client(ctx).get(f"/runs/{quote(run_id, safe='')}/diff", RunDiff)
    if as_json:
        _show(diff, "", as_json=True)
        return
    for heading, lines in (
        ("Remediation", diff.remediation),
        ("Rollback", diff.rollback),
    ):
        typer.echo(f"{heading}:")
        for line in lines or ("(none)",):
            typer.echo(f"  {printable(line)}")


@app.command("approve")
def approve(
    ctx: typer.Context,
    request_id: Annotated[str, typer.Argument(help="The approval request id.")],
    *,
    actor: ActorOption = None,
    comment: Annotated[str, typer.Option(help="Why you approve.")] = "",
    as_json: JsonOption = False,
) -> None:
    """Approve the change of an approval request. The requester cannot approve."""
    _decide(
        ctx,
        request_id,
        actor,
        {"decision": Decision.APPROVE, "comment": comment},
        as_json=as_json,
    )


@app.command("reject")
def reject(
    ctx: typer.Context,
    request_id: Annotated[str, typer.Argument(help="The approval request id.")],
    *,
    comment: Annotated[str, typer.Option(help="Why you reject. It is necessary.")],
    actor: ActorOption = None,
    as_json: JsonOption = False,
) -> None:
    """Reject the change of an approval request. One rejection rejects the run."""
    _decide(
        ctx,
        request_id,
        actor,
        {"decision": Decision.REJECT, "comment": comment},
        as_json=as_json,
    )


@app.command("cancel")
def cancel(
    ctx: typer.Context,
    request_id: Annotated[str, typer.Argument(help="The approval request id.")],
    *,
    actor: ActorOption = None,
    comment: Annotated[str, typer.Option(help="Why you cancel.")] = "",
    as_json: JsonOption = False,
) -> None:
    """Close an approval request with no decision. A parked run ends BLOCKED."""
    with _reported(as_json=as_json):
        detail = _client(ctx, actor).post(
            f"/approvals/{quote(request_id, safe='')}/cancel",
            ApprovalDetail,
            {"comment": comment},
        )
    _show(detail, _request_outcome(detail), as_json=as_json)


@approval_app.command("list")
def approval_list(
    ctx: typer.Context,
    *,
    limit: LimitOption = None,
    cursor: CursorOption = None,
    as_json: JsonOption = False,
) -> None:
    """Show the pending approval requests of the parked runs, newest first."""
    with _reported(as_json=as_json):
        page = _client(ctx).get(
            "/approvals", ApprovalPage, _params(limit=limit, cursor=cursor)
        )
    if as_json or not page.items:
        _show(page, "No pending approval requests.", as_json=as_json)
        return
    _table(
        ("REQUEST", "RUN", "REQUESTER", "NEEDED", "EXPIRES"),
        [
            (
                item.id,
                item.run_id,
                item.requester,
                str(item.required_count),
                _time(item.expires_at),
            )
            for item in page.items
        ],
    )
    _next_page("requests", page.next_cursor)


@device_app.command("list")
def device_list(
    ctx: typer.Context,
    *,
    limit: LimitOption = None,
    cursor: CursorOption = None,
    as_json: JsonOption = False,
) -> None:
    """Show the devices that the last sync wrote."""
    with _reported(as_json=as_json):
        page = _client(ctx).get(
            "/devices", DevicePage, _params(limit=limit, cursor=cursor)
        )
    if as_json or not page.items:
        _show(
            page,
            "No devices. Read the source of truth with: hco-core device sync",
            as_json=as_json,
        )
        return
    _table(
        ("DEVICE", "NAME", "PLATFORM", "ADDRESS", "SITE", "CRITICALITY"),
        [
            (
                item.sot_device_id,
                item.name,
                item.platform_name,
                item.mgmt_address,
                item.site or "-",
                item.criticality,
            )
            for item in page.items
        ],
    )
    _next_page("devices", page.next_cursor)


@device_app.command("sync")
def device_sync(
    ctx: typer.Context, *, actor: ActorOption = None, as_json: JsonOption = False
) -> None:
    """Write each device of the source of truth to the database, through the API."""
    with _reported(as_json=as_json):
        result = _client(ctx, actor).post("/devices/sync", SyncResult)
    noun = "device" if result.synced == 1 else "devices"
    _show(result, f"Synced {result.synced} {noun}.", as_json=as_json)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Upgrade the schema in HCO_DATABASE_URL, then make the checkpointer tables."""
    settings = load_settings(CoreSettings)
    try:
        refuse_development_key(settings)
    except DevelopmentKeyError as error:
        _fail(str(error))
    try:
        migrate.upgrade(settings)
        asyncio.run(setup_checkpointer(settings))
    except Exception as error:  # ruff: ignore[blind-except]
        _fail(
            f"the database upgrade failed ({type(error).__name__}, "
            f"{failure_reason(error)}); check HCO_DATABASE_URL"
        )
    typer.echo("The database schema and the checkpointer tables are ready.")


@serve_app.command("api")
def serve_api(
    host: Annotated[str, typer.Option(help="The address to listen on.")] = "127.0.0.1",
    port: Annotated[
        int, typer.Option(min=1, max=65535, help="The port to listen on.")
    ] = 8080,
) -> None:
    """Start the REST API. It reads the HCO_ variables and needs HCO_VALIDATION_KEY."""
    settings = load_settings(CoreSettings)
    try:
        application = create_app(settings, build_deps(settings))
    except (
        MissingValidationKeyError,
        ShortValidationKeyError,
        DevelopmentKeyError,
    ) as error:
        _fail(str(error))
    except AdapterConfigError as error:
        _fail(f"{error}; check HCO_SOT, HCO_SOT_PATH, and HCO_CONNECTION")
    configure_logging(settings)
    uvicorn.run(application, host=host, port=port, log_config=None)


@app.command("lint-interrupts")
def lint_interrupts(
    paths: Annotated[list[Path], typer.Argument(help="The files or folders to check.")],
) -> None:
    """Make sure that each graph node has one unconditional interrupt() or none.

    Raises:
        typer.Exit: With the exit code of the rule.

    """
    raise typer.Exit(
        interrupts.main([str(path) for path in paths], "hco-core lint-interrupts")
    )


def _decide(
    ctx: typer.Context,
    request_id: str,
    actor: str | None,
    body: dict[str, str],
    *,
    as_json: bool,
) -> None:
    with _reported(as_json=as_json):
        detail = _client(ctx, actor).post(
            f"/approvals/{quote(request_id, safe='')}/decide", ApprovalDetail, body
        )
    _show(detail, _request_outcome(detail), as_json=as_json)


def load_settings[Settings: BaseSettings](kind: type[Settings]) -> Settings:
    """Return the settings, or stop with exit code 1 and one sentence per bad variable."""
    try:
        return kind()
    except ValidationError as error:
        problems = "; ".join(
            f"HCO_{'_'.join(map(str, item['loc'])).upper()}: {item['msg']}"
            for item in error.errors()
        )
        _fail(f"a setting is not valid: {problems}")


def _client(ctx: typer.Context, actor: str | None = None) -> ApiClient:
    """Return a client of `HCO_API_URL`. A test gives a transport in `ctx.obj`.

    An `--as` that is given, also an empty one, is never replaced by `HCO_ACTOR`.
    """
    settings = load_settings(ClientSettings)
    name = settings.actor if actor is None else actor
    if name is not None and not is_valid_name(unicodedata.normalize("NFKC", name)):
        source = "HCO_ACTOR" if actor is None else "--as"
        _fail(
            f"the user name in {source} is not valid; give a name with no space "
            "at either end and no control or invisible character",
            EXIT_USAGE,
        )
    transport = ctx.obj if isinstance(ctx.obj, httpx.BaseTransport) else None
    try:
        return ApiClient(settings.api_url, name, transport)
    except ApiError as error:
        _fail(str(error))


def _request_timeout(deadline: float) -> float:
    """Return the time for one request: what remains of the wait, in bounds."""
    remaining = deadline - time.monotonic()
    return min(TIMEOUT_SECONDS, max(MIN_REQUEST_SECONDS, remaining))


def _wait(client: ApiClient, run_id: str, deadline: float) -> RunDetail:
    """Return the run when its review stops, or when the deadline passes."""
    path = f"/runs/{quote(run_id, safe='')}"
    while True:
        run = client.get(path, RunDetail, timeout=_request_timeout(deadline))
        if run.state not in WORKING_STATES or time.monotonic() >= deadline:
            return run
        time.sleep(POLL_SECONDS)


def _outcome(run: RunDetail, timeout: float) -> str:
    if run.state in WORKING_STATES:
        return (
            f"Run {run.id} is still {run.state} after {timeout:g} seconds. "
            f"Show it with: hco-core run show {run.id}"
        )
    found = f"Run {run.id} on device {run.device_name} is {run.state}."
    if run.state is RunState.AWAITING_APPROVAL:
        return (
            f"{found} Risk tier: {run.risk_tier_final}. "
            "Find its approval request with: hco-core approval list"
        )
    if run.state is RunState.BLOCKED:
        return f"{found} Show the errors with: hco-core run show {run.id}"
    return f"{found} Show it with: hco-core run show {run.id}"


def _request_outcome(detail: ApprovalDetail) -> str:
    approvals = sum(item.decision is Decision.APPROVE for item in detail.decisions)
    return (
        f"Request {detail.id} is {detail.state}: {approvals} of "
        f"{detail.required_count} approvals. Run {detail.run_id} is {detail.run_state}."
    )


def _params(**values: object) -> dict[str, str]:
    return {name: str(value) for name, value in values.items() if value is not None}


@contextmanager
def _reported(*, as_json: bool) -> Generator[None]:
    """Print an API error as one sentence and stop with exit code 1."""
    try:
        yield
    except ApiError as error:
        if as_json and error.problem is not None:
            typer.echo(json.dumps(error.problem, indent=2))
        _fail(str(error))


def _fail(sentence: str, code: int = EXIT_FAILED) -> NoReturn:
    typer.echo(f"Error: {printable(sentence).rstrip('.')}.", err=True)
    raise typer.Exit(code)


def _show(model: BaseModel, sentence: str, *, as_json: bool) -> None:
    typer.echo(model.model_dump_json(indent=2) if as_json else printable(sentence))


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    cells = [headers, *([printable(cell) for cell in row] for row in rows)]
    widths = [max(map(len, column)) for column in zip(*cells, strict=True)]
    for line in cells:
        typer.echo(
            "  ".join(
                cell.ljust(width) for cell, width in zip(line, widths, strict=True)
            ).rstrip()
        )


def _labels(*pairs: tuple[str, str]) -> None:
    for label, value in pairs:
        typer.echo(f"{label + ':':<{LABEL_WIDTH}}{printable(value)}".rstrip())


def _next_page(items: str, cursor: str | None) -> None:
    if cursor is not None:
        typer.echo(printable(f"More {items}: add --cursor {cursor}"))


def _time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
