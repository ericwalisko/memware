# memware

**Memory for AI agents that only remembers the latest truth.**

memware is one SQLite file with two stores:

- **turns** — immutable evidence. Every prompt and answer from past sessions,
  indexed with FTS5. Recall is BM25 × recency × use, ~4 ms, no model in the loop.
- **beliefs** — a bi-temporal ledger of facts. A new value for the same
  `(subject, relation)` **supersedes** the old one. Recall only ever returns the
  currently valid belief; history is kept for audit and never reaches a prompt.

No daemon, no vector database, no LLM call at capture or read time. A 30-day
corpus of a busy coding agent indexes in about five seconds into ~30 MB.

```text
$ memware sync ~/.claude/projects --harness claude-code
{"added": 14348, "files": 1475}

$ memware assert "api" "listens on port" "8443" --source "session 3f2a, turn 41"
{"outcome": "superseded", "belief_id": 2, "incumbent_id": 1}

$ memware recall "which port does the api use" --what beliefs
api listens on port 8443            # 8080 is in the ledger, retired, and never surfaces
```

## Why

Agent memory systems that rewrite what they remember degrade: continuous
LLM consolidation can push utility *below* having no memory at all
([Useful Memories Become Faulty When Continuously Updated by LLMs](https://arxiv.org/abs/2605.12978)).
And embeddings cannot tell a *contradicted* fact from a *rephrased* one — AUROC 0.59 —
so vector stores serve stale facts 15–40% of the time on evolving knowledge
([Temporal Validity in Retrieval Memory](https://arxiv.org/abs/2606.26511)).

memware borrows four mechanisms from human memory research and keeps them deliberately small:

| mechanism | in the brain | in memware |
|---|---|---|
| evidence ≠ belief | hippocampus vs neocortex (complementary learning systems) | `turn` table is append-only; `belief` table is separate |
| update on surprise | reconsolidation driven by prediction error | `memware assert` at the moment an agent notices a conflict |
| only the latest understanding | reconsolidated traces overwrite in place | deterministic supersession keyed on `(subject, relation)`, ordered by **event time** |
| need-probability recall | Anderson & Schooler 1991 / ACT-R activation | `bm25 × (1+age)^-d × (1 + w·ln(1+uses))` |

Full rationale and citations: [docs/design.md](docs/design.md).

## Install

```bash
pip install memware            # core, stdlib only (SQLite with FTS5)
pip install "memware[mcp]"     # + MCP server
```

## Use it from Claude Code

`integrations/claude-code/` is a Claude Code plugin (`claude plugin marketplace add
ericwalisko/memware`, then `claude plugin install memware@memware`). Hooks: `SessionEnd`/`PreCompact` sync the
transcript into the index; an optional `UserPromptSubmit` hook injects the handful of
currently valid beliefs relevant to the prompt (beliefs only — transcript search is
on demand through the MCP tools). See [docs/integrations.md](docs/integrations.md).

## Use it from Hermes Agent

`integrations/hermes/memware/` is a memory-provider plugin built on Hermes's
`MemoryProvider` ABC — prompt-time belief prefetch, non-blocking turn capture, and
`memware_recall` / `memware_remember` tools — sharing one store with Claude Code.

## The supersession rule

```text
same key, same value   → reinforce (reliability rises, use is counted)
same key, newer value  → supersede: incumbent gets valid_to = new.valid_from
same key, older value  → filed as history; the timeline stays consistent
weaker challenger      → parked as a candidate and sent to review
```

Ordering is decided by `valid_from` (when the evidence says it became true), never by
insertion order — so a backfill converges to the same state in any order, twice, or in
batches. Three policies: `auto` (last writer by event time), `gate_conflicts`
(default: a less reliable challenger goes to review), `await_confirmation`.

## Reviewing contested supersessions

memware does not ship a UI. It ships a contract — `ReviewBackend` with `publish()` and
`collect()` — plus two implementations: JSONL outbox/inbox files and a plain HTTP
endpoint. Wire it to whatever you already use to make decisions.

```bash
memware review sync                       # outbox ~/.memware/review-outbox.jsonl
echo '{"review_id": 7, "decision": "approve"}' >> ~/.memware/review-inbox.jsonl
memware review sync                       # applied
```

## Evaluation

`memware-eval` scores retrieval against a question set: does the right evidence
surface, and does the stale value stay hidden? It needs no model, so results are
reproducible. The protocol for end-to-end comparisons — agent alone vs agent + memware —
is in [docs/eval.md](docs/eval.md).

## Status

Alpha. The schema may change before 1.0; the ledger semantics will not.

## License

MIT. See [LICENSE](LICENSE).
