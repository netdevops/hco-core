"""The base model every domain type subclasses."""

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict


class BaseModel(PydanticBaseModel):
    """Frozen, strict base for every domain model.

    Mirrors the convention `hier_config` uses for its own rule models: frozen so
    instances are hashable and safe to share across threads and graph nodes, and
    `extra="forbid"` so a typo in a payload is an error rather than a silently
    ignored field.

    Collections on subclasses must be immutable — `tuple` and `frozenset`, never
    `list` or `set` — or the frozen guarantee is only skin deep.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
