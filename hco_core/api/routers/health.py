"""The health route: is the process up, and does the database answer."""

import asyncio
from typing import Final, Literal

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from hco_core import __version__
from hco_core.api.deps import ApiDeps
from hco_core.api.errors import DatabaseUnavailableError
from hco_core.domain.base import BaseModel

DATABASE_TIMEOUT_SECONDS: Final = 5.0


class Health(BaseModel):
    """The status of the process and of the database."""

    status: Literal["ok"]
    database: Literal["ok"]
    version: str


def health_router(deps: ApiDeps) -> APIRouter:
    """Return the router of `GET /health`."""
    router = APIRouter(tags=["health"])

    @router.get("/health")
    async def health() -> Health:  # pyright: ignore[reportUnusedFunction]
        """Return the status when the database answers.

        Raises:
            DatabaseUnavailableError: If the database does not answer in time.

        """
        try:
            async with (
                asyncio.timeout(DATABASE_TIMEOUT_SECONDS),
                deps.sessions() as session,
            ):
                await session.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise DatabaseUnavailableError from error
        return Health(status="ok", database="ok", version=__version__)

    return router
