"""Retrieval over runbooks, documentation, and past-change precedents."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from hco_core.domain.knowledge import VectorDocument, VectorHit


@runtime_checkable
class VectorStore(Protocol):
    """Stores and retrieves documents by similarity."""

    async def upsert(
        self, collection: str, documents: Sequence[VectorDocument]
    ) -> None:
        """Insert or replace documents."""
        ...

    async def search(
        self,
        collection: str,
        query: str,
        *,
        k: int = 5,
        filters: Mapping[str, Any] | None = None,
    ) -> tuple[VectorHit, ...]:
        """Retrieve the most similar documents."""
        ...

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        """Remove documents."""
        ...
