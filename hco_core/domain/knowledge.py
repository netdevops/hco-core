"""Documents in a vector store: runbooks, precedents, incidents."""

from collections.abc import Mapping
from typing import Any

from hco_core.domain.base import BaseModel


class VectorDocument(BaseModel):
    """A document to embed and retrieve.

    The `precedents` collection is the one that compounds. Every completed run
    writes a document recording the change, the risk tier, the decision, and the
    outcome -- so a deployment that has been running six months gives better
    context than a new one.
    """

    id: str
    text: str
    metadata: Mapping[str, Any] = {}


class VectorHit(BaseModel):
    """One retrieval result."""

    id: str
    text: str
    score: float
    metadata: Mapping[str, Any] = {}
