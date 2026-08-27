# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added

- Initial release: nine ports and the domain models they speak.
- `RemediationPlanner`, the AI contract, with invariants C1 through C8.
- `LLMProvider` for model transport and discovery, deliberately separate from
  `RemediationPlanner` — conflating "which model" with "what the AI may do" is
  how a model ends up able to emit commands.
- `ApplyStrategy.reverts_without_orchestrator`, which makes it checkable rather
  than assumed whether a platform can take part in an atomic change set.
- `TopologyGraph.is_single_homed()` and `.shares_group()`, the two questions a
  deterministic risk floor asks most often.
