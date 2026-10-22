"""The dependencies of the REST API, and how to build them from the settings."""

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hco_core.adapters import registry
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.db.types import ULID_LENGTH
from hco_core.domain.approval import ApprovalDecision, ApprovalRequest
from hco_core.graphs.review import ApprovalDeps, AssessmentHook
from hco_core.services.approval import ApprovalService
from hco_core.services.ledger import NotificationLedger
from hco_core.services.runs import RunRepository
from hco_core.settings import CoreSettings

API_PREFIX: Final = "/api/v1"
DEFAULT_PAGE: Final = 50
MAX_PAGE: Final = 500
ULID_PATTERN: Final = rf"^[0-9A-HJKMNP-TV-Z]{{{ULID_LENGTH}}}$"
"""A ULID in Crockford base 32, as `new_id` makes it."""

type Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
"""The most items in one page."""
type Cursor = Annotated[str | None, Query(pattern=ULID_PATTERN)]
"""The `next_cursor` of the page before, or nothing for the first page."""


class MissingValidationKeyError(ValueError):
    """`HCO_VALIDATION_KEY` is not set."""

    def __init__(self) -> None:
        super().__init__("set HCO_VALIDATION_KEY to at least 32 random bytes")


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiDeps:
    """The database and the review graph dependencies that the routes use.

    The app disposes of the engine when it stops.
    """

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    review: ApprovalDeps
    assessment: AssessmentHook | None = None


def build_deps(settings: CoreSettings) -> ApiDeps:
    """Build the dependencies from the adapter names and options of the settings.

    Raises:
        MissingValidationKeyError: If `HCO_VALIDATION_KEY` is not set.

    """
    if settings.validation_key is None:
        raise MissingValidationKeyError
    engine = create_database_engine(settings)
    sessions = create_sessionmaker(engine)
    review = ApprovalDeps(
        sot=registry.source_of_truth(settings.sot, {"path": str(settings.sot_path)}),
        secrets=registry.secrets("env", {}),
        connection=registry.connection(
            settings.connection,
            {"transcripts_dir": str(settings.transcripts_dir)},
            token_key=settings.validation_key.get_secret_value().encode(),
        ),
        runs=RunRepository(sessions),
        approvals=ApprovalService(sessions, resume=resume_not_wired),
        ledger=NotificationLedger(
            sessions, {"log": registry.notification_channel("log", {})}
        ),
    )
    return ApiDeps(engine=engine, sessions=sessions, review=review)


async def resume_not_wired(  # ruff: ignore[unused-async]
    request: ApprovalRequest, decision: ApprovalDecision | None
) -> None:
    """Refuse to resume a run. TODO(netdevops/hco-core#11): wire `RunResumer`.

    Raises:
        NotImplementedError: Always. No route of this API decides a request.

    """
    del request, decision
    message = "the approvals route arrives with netdevops/hco-core#11"
    raise NotImplementedError(message)
