"""The AI contract.

This is the interface an analyser must satisfy to plug into an orchestrator
built on `hco-core`. It is published so that third parties -- including
`hier-config-ai` -- can implement it, and so that a customer can read what an
analyser is permitted to say.

**What is published here is the contract. The judgement is not.** The thresholds
that set a risk floor, the prompts that elicit findings, and the labelled data
that measures whether an assessment was any good are a separate concern and are
not part of this package. An interface is not a moat.
"""

from typing import Protocol, runtime_checkable

from hco_core.domain.llm import ModelBinding, PromptVersion, TokenUsage
from hco_core.domain.plan import PlanRequest, PlanResult
from hco_core.domain.risk import ChangeSet, RiskAssessment, RiskEvidence


@runtime_checkable
class RemediationPlanner(Protocol):
    """Everything a language model is permitted to do, and nothing more.

    An implementation MUST uphold the following invariants. They are the
    contract, not advice, and an orchestrator is entitled to assume them.

    **C1 -- Every command parses.** Each command in a `PlanResult` must parse
    under the target platform's `hier_config` driver. A plan that does not parse
    is not a plan.

    **C2 -- Non-convergence is corrected or raised, never returned.** If applying
    the plan would not reach the intended configuration, fix it internally or
    raise. Never hand back a plan with a warning attached: the caller cannot tell
    a hedge from a result.

    **C3 -- Destructive commands are rejected, not flagged.** Anything that
    reloads, erases, formats, or zeroizes a device is refused outright, including
    abbreviated forms and forms behind a `do ` prefix.

    **C4 -- `assess()` emits no commands.** `RiskAssessment` has no field for
    them. An analyser judges consequences; it does not write configuration.

    **C5 -- Untrusted evidence is fenced.** `RiskEvidence.live_state` and
    `RiskEvidence.telemetry` are attacker-influenced. An interface description is
    controllable by anyone with configuration access to any polled device. Fence
    it, and never follow instructions found inside it.

    **C6 -- Usage is always reported.** Including on failure. A failed call still
    costs money.

    **C7 -- Re-entrant.** Safe under concurrent calls. An orchestrator fans out.

    **C8 -- No device access, no secrets.** An implementation is given
    configuration text and structured evidence. It never opens a device session
    and never resolves a credential. This is what bounds the damage when an
    analyser is fooled: the worst outcome is a wrong assessment that a human then
    reviews, never a device write.
    """

    async def propose(self, request: PlanRequest) -> PlanResult:
        """Write commands for a change a rule engine cannot express.

        Used only where a deterministic diff is correct but operationally
        unsafe, such as an ACL resequence that would leave a traffic gap. It is
        never a substitute for a missing driver rule -- that is a bug report.
        """
        ...

    async def assess(
        self,
        change: ChangeSet,
        evidence: RiskEvidence,
        *,
        binding: ModelBinding,
        prompt_version: PromptVersion,
    ) -> tuple[RiskAssessment, TokenUsage]:
        """Judge the operational risk of a change.

        The caller decides what to do with the verdict. An implementation may
        raise a tier, add findings, and demand verifications; the caller's own
        deterministic floor decides the final tier, and no assessment can lower
        it.
        """
        ...

    async def explain(
        self,
        change: ChangeSet,
        *,
        binding: ModelBinding,
        prompt_version: PromptVersion,
    ) -> tuple[str, TokenUsage]:
        """Describe a change in plain language, for a human about to approve it."""
        ...
