"""The FastAPI application of hco-core.

Run it with `uvicorn --factory hco_core.api.app:app_from_env`.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog
from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from hco_core import __version__
from hco_core.api.deps import API_PREFIX, ApiDeps, build_deps
from hco_core.api.errors import add_problem_handlers
from hco_core.api.routers.devices import devices_router
from hco_core.api.routers.health import health_router
from hco_core.logging import configure_logging
from hco_core.settings import CoreSettings


@dataclass(frozen=True, slots=True)
class RequestIdMiddleware:
    """Bind a new request id to every log line of one HTTP request."""

    app: ASGIApp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the request with the id bound, so the access line also carries it."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with structlog.contextvars.bound_contextvars(request_id=str(uuid.uuid4())):
            await self.app(scope, receive, send)


class CoreAPI(FastAPI):
    """Bind the request id outside the error handler of Starlette."""

    def build_middleware_stack(self) -> ASGIApp:
        """Wrap the full stack, so a 500 access line also carries the request id."""
        return RequestIdMiddleware(super().build_middleware_stack())


def create_app(settings: CoreSettings, deps: ApiDeps) -> FastAPI:
    """Build the application on the dependencies. It disposes of the engine at stop.

    A router that starts a run reads the checkpointer settings.
    """
    del settings

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await deps.engine.dispose()

    application: FastAPI = CoreAPI(
        title="hco-core", version=__version__, lifespan=lifespan
    )
    add_problem_handlers(application)
    application.include_router(health_router(deps))
    application.include_router(devices_router(deps), prefix=API_PREFIX)
    return application


def app_from_env() -> FastAPI:
    """Build the application from the `HCO_` variables, and configure logging."""
    settings = CoreSettings()
    configure_logging(settings)
    return create_app(settings, build_deps(settings))
