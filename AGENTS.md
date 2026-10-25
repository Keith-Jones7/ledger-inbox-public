# Ledger Inbox Agent Instructions

- Read [README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md) and
  [docs/operations.md](docs/operations.md) before changing behavior.
- Preserve the invariants in CONTRIBUTING: persist image bytes before
  acknowledging ingress, scope deduplication and credentials to one tenant,
  persist recognition before a ledger write, and never automatically replay an
  ambiguous external write.
- Keep transports, recognizers and ledger sinks behind their adapter
  interfaces; do not couple the core state machine to one provider.
- Never add receipt images, email addresses, API keys, tokens or real financial
  records to fixtures, logs, docs or commits.
- This repository and its public release mirror carry identical trees. Apply a
  change to both and keep the commit messages aligned.

## Verification

Match CI before committing:

```bash
uv sync --locked --extra dev
uv run ruff check src tests scripts examples
uv run ruff format --check src tests scripts examples
uv run pytest
uv run python scripts/smoke.py
uv build
```

Changes to the container, persistence or recovery also need the compose smoke
test from `.github/workflows/ci.yml`.
