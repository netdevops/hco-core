"""The result of the validation gate, and the freshness verdict of a snapshot."""

from enum import StrEnum
from typing import Self

from pydantic import model_validator

from hco_core.domain.base import BaseModel


class FreshnessVerdict(StrEnum):
    """Whether a stored running config is safe to act on at a risk tier."""

    USE_SNAPSHOT = "use_snapshot"
    RECOLLECT = "recollect"


class ValidationOutcome(BaseModel):
    """What `validate_plan` decided about one plan.

    A passed plan has a `token` and no `failed_check`. A blocked plan has the
    number of the first check that failed, its problem, and no `token`.
    """

    failed_check: int | None = None
    problems: tuple[str, ...] = ()
    commands_requiring_review: tuple[str, ...] = ()
    """The plan lines that start with the negation prefix of the driver."""
    scaffolding: tuple[str, ...] = ()
    """The plan lines that add a command and then remove it, in plan order."""
    token: str | None = None
    """The HMAC-SHA256 hex digest of the canonical plan bytes."""

    @property
    def ok(self) -> bool:
        """True if the plan passed every check."""
        return self.failed_check is None

    @model_validator(mode="after")
    def _token_only_when_passed(self) -> Self:
        passed = self.failed_check is None
        if passed != (self.token is not None) or passed == bool(self.problems):
            message = "a passed outcome has a token and no problems, a blocked one not"
            raise ValueError(message)
        return self
