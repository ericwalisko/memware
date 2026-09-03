# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.1] - 2026-09-03

### Added
- Hermes memory-provider plugin implementing the `MemoryProvider` ABC (prefetch, non-blocking sync_turn, session flush, built-in memory mirroring, four recall/remember tools); the repo is a Claude Code plugin marketplace (`claude plugin marketplace add ericwalisko/memware`).
- Turn hits carry `snippet`: the FTS5 window around the matched terms (the head of a long turn often lacks the answer). Default window 96 tokens; `memware recall --snippet-tokens N`.
- Release workflow that publishes to PyPI from a `v*` tag via trusted publishing
  (OIDC, no stored token). It installs the built wheel into a clean venv and refuses
  to upload when the tag and the package version disagree; `RELEASING.md` documents
  the one-time pypi.org setup and the per-release steps.

### Changed
- Stale scoring is positional: an answer is stale only when the old value appears before the current one.
- `memware-eval` mirrors the hook's subject gate for the beliefs context and reports the belief injection rate (overall and on negatives).
- `memware-eval` now scores two contexts per question: `beliefs` (what a prompt-time hook injects) and `beliefs+turns`; negatives pass when the expected value is absent rather than when the context is empty.
- The package version is read from `memware.__version__` by the build backend, so
  `memware --version` and the published distribution cannot disagree.

## [0.1.0] - 2026-09-02

### Added
- Bi-temporal belief ledger with deterministic `(subject, relation)` supersession,
  event-time ordering, reinforcement, and reliability-gated review.
- FTS5 transcript index with BM25 × activation ranking.
- Ingest adapters for Claude Code session JSONL and generic message JSONL, with
  idempotent byte-offset cursors.
- `memware` CLI, optional MCP server, and retrieval-level evaluation runner.
- `ReviewBackend` contract with JSONL and HTTP implementations.
- Claude Code hook bundle and Hermes memory-provider skeleton.
