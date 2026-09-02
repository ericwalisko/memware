# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
