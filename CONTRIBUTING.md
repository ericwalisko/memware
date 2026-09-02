# Contributing

Thanks for considering a contribution. Small, focused pull requests are easiest to review.

## Development

```bash
git clone https://github.com/ericwalisko/memware && cd memware
uv venv && uv pip install -e ".[dev,mcp]"     # or: python -m venv .venv && pip install -e ".[dev,mcp]"
ruff check src tests && ruff format --check src tests
mypy
pytest
```

CI runs the same commands on Python 3.11, 3.12 and 3.13.

## Ground rules

- **Tests first.** Every behaviour change comes with a test that fails without it.
  Fixtures are synthetic — never commit real transcripts or memories.
- **The ledger semantics are a contract.** Changes to supersession ordering, policies,
  or what `recall` may return need a design note in `docs/design.md` in the same PR.
- **No model in the read path.** Proposals that add an LLM call to capture or recall
  should be framed as optional, off-by-default extras.
- Keep the core dependency-free. New runtime dependencies go behind an extra.

## Sign-off (DCO)

This project uses the [Developer Certificate of Origin](https://developercertificate.org/).
Sign each commit with `git commit -s`, which adds `Signed-off-by: Your Name <email>`.
No CLA.

## Reporting bugs and proposing features

Use the issue templates. For security issues, see [SECURITY.md](SECURITY.md).

## Code of conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
