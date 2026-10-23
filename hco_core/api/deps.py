"""The dependencies of the REST API, and how to build them from the settings."""

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Query
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hco_core.adapters import registry
from hco_core.db.session import create_database_engine, create_sessionmaker
from hco_core.db.types import ULID_LENGTH
from hco_core.engine.validation import MIN_KEY_BYTES
from hco_core.graphs.review import ApprovalDeps, AssessmentHook
from hco_core.services.ledger import NotificationLedger
from hco_core.services.resume import review_with_resume
from hco_core.services.runs import RunRepository
from hco_core.settings import DEVELOPMENT_VALIDATION_KEY, CoreSettings

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


class ShortValidationKeyError(ValueError):
    """`HCO_VALIDATION_KEY` is shorter than `MIN_KEY_BYTES`."""

    def __init__(self) -> None:
        super().__init__(
            f"HCO_VALIDATION_KEY must have at least {MIN_KEY_BYTES} bytes; "
            'make a new key with: python -c "import secrets; '
            'print(secrets.token_hex(32))"'
        )


class DevelopmentKeyError(ValueError):
    """`HCO_VALIDATION_KEY` is the public development key, with a real connection."""

    def __init__(self, connection: str) -> None:
        super().__init__(
            f"HCO_VALIDATION_KEY is the public key of development/.env, and "
            f"HCO_CONNECTION is {connection!r}; only the mock connection accepts it. "
            'Make a new key with: python -c "import secrets; '
            'print(secrets.token_hex(32))"'
        )


def refuse_development_key(settings: CoreSettings) -> None:
    """Refuse the public development key for a connection other than `mock`.

    Raises:
        DevelopmentKeyError: If the key is the development key and the
            connection is not `mock`.

    """
    key = settings.validation_key
    if (
        key is not None
        and key.get_secret_value() == DEVELOPMENT_VALIDATION_KEY
        and settings.connection != "mock"
    ):
        raise DevelopmentKeyError(settings.connection)


def validation_key(settings: CoreSettings) -> bytes:
    """Return the key that signs and verifies the plan tokens.

    `refuse_development_key` also refuses the public development key.

    Raises:
        MissingValidationKeyError: If `HCO_VALIDATION_KEY` is not set.
        ShortValidationKeyError: If the key is shorter than `MIN_KEY_BYTES`.

    """
    if settings.validation_key is None:
        raise MissingValidationKeyError
    refuse_development_key(settings)
    key = settings.validation_key.get_secret_value().encode()
    if len(key) < MIN_KEY_BYTES:
        raise ShortValidationKeyError
    return key


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiDeps:
    """The database and the review graph dependencies that the routes use.

    The app disposes of the engine when it stops. Build `review` with
    `review_with_resume` and the same `assessment`, so a decision resumes runs.
    """

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    review: ApprovalDeps
    assessment: AssessmentHook | None = None


def build_deps(settings: CoreSettings) -> ApiDeps:
    """Build the dependencies from the adapter names and options of the settings.

    `validation_key` refuses a missing key and the development key with a
    real connection.
    """
    token_key = validation_key(settings)
    engine = create_database_engine(settings)
    sessions = create_sessionmaker(engine)
    review = review_with_resume(
        settings,
        sessions,
        lambda approvals: ApprovalDeps(
            sot=registry.source_of_truth(
                settings.sot, {"path": str(settings.sot_path)}
            ),
            secrets=registry.secrets("env", {}),
            connection=registry.connection(
                settings.connection,
                {"transcripts_dir": str(settings.transcripts_dir)},
                token_key=token_key,
            ),
            runs=RunRepository(sessions),
            approvals=approvals,
            ledger=NotificationLedger(
                sessions, {"log": registry.notification_channel("log", {})}
            ),
        ),
    )
    return ApiDeps(engine=engine, sessions=sessions, review=review)
