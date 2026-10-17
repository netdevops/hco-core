# Summary

<!-- What does this pull request change, and why? Link the issue, for example "Closes #123". -->

## Self-review checklist

- [ ] I wrote the tests first, and they cover the change.
- [ ] `poetry run ruff format --check .` and `poetry run ruff check .` pass.
- [ ] `poetry run mypy hco_core` and `poetry run pyright` pass.
- [ ] `poetry run pytest --cov=hco_core` passes (95% coverage).
- [ ] I added a fragment in `changes/`. See `changes/README.md`.
- [ ] I say here if I touched the validation gate or anything that reaches a device.

## AI-assisted contributions

If an AI coding tool wrote this pull request, review it against the checklist above before you ask for a review.
