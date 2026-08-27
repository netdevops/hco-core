"""Model transport.

Deliberately separate from `RemediationPlanner`. Conflating "which model" with
"what the AI is allowed to do" is how a model ends up able to emit commands;
splitting them is what makes the monotonic-risk rule enforceable.
"""

from typing import Any, Protocol, runtime_checkable

from hco_core.domain.llm import ModelBinding, ModelInfo, ProviderKind
from hco_core.domain.observability import HealthStatus


@runtime_checkable
class LLMProvider(Protocol):
    """A source of language models."""

    @property
    def kind(self) -> ProviderKind:
        """Which provider this is."""
        ...

    async def discover_models(self) -> tuple[ModelInfo, ...]:
        """Enumerate the models this provider currently offers.

        A snapshot, not a contract. Providers add, remove, and silently change
        models between calls.
        """
        ...

    async def health(self) -> HealthStatus:
        """Check reachability and authentication."""
        ...

    def chat_model(self, binding: ModelBinding) -> Any:  # ruff: ignore[any-type]
        """Return a client configured for this binding.

        Typed `Any` on purpose: `hco-core` does not depend on any particular
        model framework, so an implementation may return a LangChain
        `BaseChatModel`, a PydanticAI `Model`, or its own client.
        """
        ...
