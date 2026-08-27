"""Language-model transport: providers, models, bindings, and cost."""

from datetime import datetime
from enum import StrEnum

from hco_core.domain.base import BaseModel
from hco_core.domain.ids import BindingID, PromptVersionID
from hco_core.domain.secrets import SecretRef


class ProviderKind(StrEnum):
    """A model provider.

    The self-hosted kinds are first-class rather than a fallback. Many network
    teams forbid sending device configuration to a third party, and a running
    configuration is full of addressing, topology, community strings, and
    credential hashes.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"
    GOOGLE = "google"
    GROQ = "groq"
    MISTRAL = "mistral"
    OLLAMA = "ollama"
    VLLM = "vllm"
    OPENAI_COMPAT = "openai_compat"

    @property
    def is_self_hosted(self) -> bool:
        """True when the model can run inside the customer's own network."""
        return self in {ProviderKind.OLLAMA, ProviderKind.VLLM}


class ModelInfo(BaseModel):
    """One model a provider offers.

    Discovery is a snapshot, not a contract. A provider can add, remove, or
    silently change a model between calls, which is why a binding used on a
    change-approval path pins a version.
    """

    model_id: str
    display_name: str = ""
    context_window: int | None = None
    supports_tools: bool | None = None
    supports_structured_output: bool | None = None
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    discovered_at: datetime | None = None


class ModelBinding(BaseModel):
    """A named, pinned model configuration for one purpose."""

    id: BindingID
    name: str
    purpose: str
    provider_kind: ProviderKind
    model_id: str
    base_url: str | None = None
    api_key_ref: SecretRef | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    timeout_seconds: float = 120.0
    pinned_version: str | None = None
    """An exact model snapshot.

    Providers change behaviour behind a moving alias without notice, and a
    silent change to a model that gates production changes is a silent change to
    the gate.
    """


class PromptVersion(BaseModel):
    """One version of a prompt, content-addressed so a judgement is reproducible."""

    id: PromptVersionID
    slug: str
    version: int
    content_sha256: str
    is_active: bool = False


class TokenUsage(BaseModel):
    """What one model call consumed.

    Always reported, including on failure. A failed call still costs money, and
    a budget that only counts successes is not a budget.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    estimated_cost: float = 0.0
    model_id: str = ""
    latency_seconds: float = 0.0
