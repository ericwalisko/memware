# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Passage-level index.** Turns are split into ~300-500-token passages at ingest
  (`memware.passage`), anchored by turn id and character offset, and FTS5 now indexes
  passages instead of whole turns. Recall ranks passages, collapses to one hit per turn
  and quotes only that turn's best matching passages (`passages_per_turn`, default 3);
  `read_session` still returns whole turns. On a 24-question fact set this held accuracy
  exactly (fact 24/24, stale 8/8, stale_rate 0) while halving the retrieved context,
  30,403 -> 15,410 characters. The database is ~1.8x larger, since a passage stores its
  own text.
- Existing stores migrate on first open (`PRAGMA user_version` 0 -> 1): `turn_fts` is
  dropped and every turn on disk is chunked, ~5 s for an 18k-turn corpus.
- `memware-eval` reports the median retrieved context size per question.
- Hermes memory-provider plugin implementing the `MemoryProvider` ABC (prefetch, non-blocking sync_turn, session flush, built-in memory mirroring, four recall/remember tools); the repo is a Claude Code plugin marketplace (`claude plugin marketplace add ericwalisko/memware`).
- Turn hits carry `snippet`: the FTS5 window around the matched terms (the head of a long turn often lacks the answer). Default window 96 tokens; `memware recall --snippet-tokens N`.

### Changed
- Stale scoring is positional: an answer is stale only when the old value appears before the current one.
- `memware-eval` now scores two contexts per question: `beliefs` (what a prompt-time hook injects) and `beliefs+turns`; negatives pass when the expected value is absent rather than when the context is empty.

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
