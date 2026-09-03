# Design

memware is built on a small number of findings from human-memory research, each
of which has a recent AI-agent paper behind it. This page records the mapping so
that changes to the ledger semantics can be argued against the evidence rather
than taste.

## 1. Evidence and belief are different stores

**Neuroscience.** Complementary Learning Systems: the hippocampus stores
episodes fast and verbatim; the neocortex learns slowly and generally; replay
during sleep moves knowledge from one to the other. You do not re-experience an
episode to know a fact.

**Agents.** *Useful Memories Become Faulty When Continuously Updated by LLMs*
(arXiv:2605.12978) shows that systems which keep consolidating raw experience
into a textual memory bank see utility rise, then fall below the no-memory
baseline; identical trajectories yield different memories depending on update
schedule. Episodic-only retention matched or beat the consolidators.

**memware.** The `turn` table is append-only and never rewritten. Beliefs live
in a separate table. Consolidation — however you do it — is gated and never
modifies evidence.

## 2. Updating happens at retrieval, on surprise

**Neuroscience.** A recalled memory becomes labile and is re-stored modified;
what triggers modification is prediction error at reactivation (Sinclair &
Barense, *Trends in Neurosciences* 2019; Phil. Trans. R. Soc. B 2026: memory
strength at reactivation, not age, governs updating).

**memware.** `memware assert` is designed to be called at the moment an agent
recalls a belief and observes a conflict. The MCP `remember` tool is the same
entry point. There is no scheduled rewrite of beliefs.

## 3. Only the most recent understanding

**Agents.** *Temporal Validity in Retrieval Memory* (MemStrata, arXiv:2606.26511):
a deterministic supersession rule on a `(subject, relation, object)` key with a
bi-temporal ledger reaches 0.95–1.00 accuracy on evolving knowledge where RAG
reaches 0.20–0.47, and cuts stale-fact serving from 15–40% to ~0%. The
critical measurement: cosine similarity separates a contradicted fact from a
rephrased duplicate at AUROC 0.59. Embedding-only stores cannot do this.
*TOKI* (arXiv:2606.06240) adds the distinction between valid time and
transaction time and keeps the losing fact in an audit row.

**memware.** `belief.key = normalize(subject) | normalize(relation)`.
Ordering is by `valid_from` (event time); `recorded_at` is transaction time.
A newer value retires the incumbent by setting `valid_to`; an older value
arriving late is filed into the timeline. Recall filters `valid_to IS NULL AND
status = 'committed'`. Superseded rows are never deleted and never surface.

## 4. Need-probability recall

**Cognitive science.** Anderson & Schooler (1991) showed the probability a
memory is needed is a power function of how recently and how often it was
needed; ACT-R's base-level activation is `ln(sum(t_j^-d))`. Retrieval itself
strengthens a trace (the testing effect).

**Agents.** *Learning What to Remember* (arXiv:2606.12945) finds recency alone
retains 36.8% of important evidence and that reliability and self-relevance
dominate learned weights. The LLM-rated "importance" term from Generative
Agents is the expensive, noisy one.

**memware.** `score = bm25 × (1 + age_days)^-decay × (1 + w·ln(1 + use_count))`
for turns, times `reliability` for beliefs. Every retrieval updates
`use_count`/`last_used`. No model call.

## 5. Gist points at verbatim

**Cognitive science.** Fuzzy-trace theory: gist and verbatim are separate
traces; gist persists and drives decisions; false memories are gist recalled
without its source.

**memware.** A belief is one sentence plus a `source` pointer. When it
matters, verify against the pointer — or against the code. Do not store what
can be derived.

The same split runs one level down inside evidence. A turn is the verbatim
record and stays whole; the unit that gets *ranked and quoted* is a ~400-token
passage carrying its turn id and character offset. Recall returns passages,
`read_session` returns turns, and the pointer between them is exact. On a
24-question fact set this matched whole-turn recall (24/24) on half the
retrieved context — the win is token cost, not accuracy: an answer BM25 could
already find is now delivered without the 4,000 characters around it.

## 6. Candidate vs committed

**Agents.** *MemTX* (arXiv:2607.23929): actions should gate on validated
beliefs, not on every accepted write.

**memware.** Under `gate_conflicts` (default), a challenger with lower
reliability than the incumbent is stored as `status='candidate'` and a review
is opened. Candidates never reach recall. Under `await_confirmation` every
conflict is reviewed; under `auto` the newest event time wins.

## Non-goals

- A knowledge graph as the truth layer. Entities and multi-hop belong in a wiki
  or a graph you already have; memware keeps the fact ledger flat.
- Per-turn LLM extraction at capture. If you derive beliefs with a model, do it
  in a gated batch and write candidates.
- Auto-recall of transcript search on every prompt. Beliefs, yes (small,
  bounded); transcript search on demand.
