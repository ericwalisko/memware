---
name: garden
description: Measure and tune what memware injects at prompt time, then tend the belief ledger. Blind-label recent prompt-fact pairs from the relevance log, score the relevance filter (relevant facts kept, dropped, added, missed), replay past prompts to find the jointly best threshold, per-prompt cap and pool, move toward it once held-out prompts confirm it, and propose fixes to the beliefs that keep arriving as noise, plus stale, orphaned, duplicate and vaguely-named ones. Use when asked to garden, tune, calibrate or audit memware, Jev or the relevance filter, check whether injected memories are relevant, clean up beliefs, or on a regular (about weekly) upkeep run.
---

# memware garden

One cycle has two parts:

- **Part A** measures what the prompt hook injected, finds the best settings by replaying past
  prompts, and moves toward them.
- **Part B** tends the ledger the hook injects from.

Part A needs the relevance filter in `shadow` or `filter` mode, because only those modes log the
pairs. With `relevance.mode off`, do Part B alone, and say that turning on `shadow` is how to
measure injection. Shadow sends prompts to TypeSafe
([README](../../../../README.md#optional-a-relevance-filter-for-prompt-time-injection)), so leave
that choice to the user.

`garden.py` in this skill's directory does every step that should give the same answer each
time. Run it with `python3`. It needs only the standard library and finds the memware home the
way memware does (`MEMWARE_HOME`, else `~/.memware`). It prints counts and never prompt or fact
text.

## Ground rules

- **Prompts are private.** The log and the batches hold the user's prompts. Show the user
  aggregates and belief text (the belief ledger is theirs to see), never another prompt's text.
  Labeling agents report counts only.
- **Nothing in the ledger changes without the user's yes.** Every retraction and re-assertion is
  proposed in one table and applied only after approval. Use dry runs first
  (`memware beliefs retract … ` without `--apply`).
- **Settings move only on evidence that held.** `garden.py` may move the threshold, `k` and the
  pool together, but only toward an optimum that also wins on held-out prompts, and only as far as
  the smallest step that improves. Ask before `memware config`. Never change `relevance.model`
  without a cycle in `shadow` on the new model first, because the scores differ between versions.
- **Cost:** two blind passes over at most 600 pairs, plus a third pass on the pairs where they
  disagree. That is about 300–400k tokens a cycle. Skip the cycle when there is not enough new
  data (step A1).

## Part A: measure and tune

**A1. Health.** Run `python3 garden.py health --days 14`. Report the following:

- **Reliability:** prompts, the fallback rate by cause, and p50/p95 latency.
- **Cost:** the spend over the window.
- **Block size:** facts per prompt from memware alone and with the filter.
- **Emptied blocks:** prompts where the filter dropped everything.
- **Cap-bound prompts:** prompts where more facts cleared the threshold than `inject.k` let in.

If `prompts_with_unlabeled_pairs` is under 150 and the last cycle was under 7 days ago, stop
here and do Part B only.

**A2. Sample.** Run `python3 garden.py sample --work <home>/labels/work-<YYYY-MM-DD> --days 14`.

- **What it picks:** prompts that aren't yet complete for the replay. For each one it takes
  every pair that any setting on the grid could inject (threshold 0.20–0.60, `k` 3–10, pool 10
  up to the logged size), memware's own picks, and one candidate below the grid, so misses show.
  A prompt counts in the replay only once all of these are labeled.
- **What it writes:** blind batches of at most 600 pairs, and a hidden `key.jsonl`. Labelers
  never see `key.jsonl`, and they never see a score.
- **Where:** `<home>/labels/`, so `memware nuke` removes it.

**A3. Label.** For each batch, start two agents in parallel (passes A and B). Give each the
prompt in [labeler.md](labeler.md):
- `{BATCH}` is the batch file.
- `{OUT}` is `<work>/labels/<batch stem>-A.jsonl` (or `-B`).
- `{SCRATCH}` is `<work>/tmp/<batch stem>-A/`, a directory of the agent's own.

When they finish, run `python3 garden.py disputes --work <work>`. It writes `batchdisputes.jsonl`
with only the pairs A and B disagreed on. One more agent labels that file as pass C, writing to
`<work>/labels/batchdisputes-C.jsonl`. If an agent fails or is refused, don't reissue its work
in another form. Score with the passes you have: a pair left split counts as neither relevant
nor noise.

**A4. Score.** Run `python3 garden.py score --work <work> --days 14 --record`.

- **Coverage check:** it first refuses any label file that doesn't cover exactly its own
  batch, each pair once.
- **Verdicts:** it appends majority verdicts to `<home>/labels/garden/verdicts.jsonl`, which
  holds pair ids and verdicts and no text. The next cycle then labels only new pairs.
- **The report:** it covers every verdict in the window, not only this cycle's, so the evidence
  builds up.
- **History:** it appends one line of numbers to `<home>/labels/garden/history.jsonl`.

Show the user:

| | facts | relevant | noise | precision |
|---|---|---|---|---|
| memware alone (`kept` + `dropped`) | | | | |
| with the filter (`kept` + `added`) | | | | |

Add these, then the proposals:

- **Dropped:** how relevant the dropped facts were. This should be near 0%.
- **Added:** how relevant the added facts were.
- **Misses:** relevant facts in `passed`, the candidates the filter chose not to inject.
- **Replay:** the `replay` block. It holds the current settings and the optimum, with the
  relevant and noise facts each injects on the tuning prompts; the held-out check; and the move,
  or the reason there is none.
- **Trend:** compare with the previous lines of `history.jsonl` when there are any. One cycle's
  change is not a trend. Act on a direction that holds across two or more cycles.

**A5. Apply.** Put each proposal to the user with its `why`. On a yes, run
`memware config <name> <value>` for each setting in it (`relevance.threshold`, `inject.k`,
`relevance.pool`). If the user runs the Hermes provider, `inject.k` doesn't reach it: Hermes keeps
its own `prefetch_k` in `~/.hermes/memware.json`, so offer to set that to the same number. The
next cycle's history line records the new settings beside the numbers they produced.

How the replay decides, all in `replay()` and `propose()` in `garden.py`:

- **The objective:** the least noise among settings that keep at least 99% of the relevant facts
  the current settings inject, and then the most relevant facts. The user chose this: lose
  almost nothing.
- **Why a replay works:** the log keeps the filter's score for every candidate in the pool, so
  any threshold, `k` and pool up to the logged size can be recomputed on past prompts without
  asking the filter again.
- **The held-out check:** the optimum is chosen on older prompts. The newest cycle's prompts are
  held out (a fixed third of the prompts, until there are enough cycles). The move must lose at
  most 1% of relevant facts there, and do better on the objective, or nothing moves.
- **Damping:** the move is the best setting between the current one and halfway to the optimum
  on every axis. When nothing there improves, it is the nearest setting that does. The prompt
  mix drifts from week to week, and small steps keep one week's labels from overshooting.
- **Minimums:** at least 100 complete prompts to tune on, 40 held out, and 50 relevant facts
  injected by the current settings.
- **Pool exploration:** the replay can't see past the logged pool. Once the current settings are
  the optimum and at least 5% of its relevant facts come from the deepest five logged ranks, it
  proposes a pool 10 wider for one cycle. That sends about half again as many tokens, and the
  filter's scores can shift with more candidates beside them. The next replay then covers the
  wider pool and will move it back if the extra depth doesn't pay.
- **Timeout:** up 0.5 s, to at most 5, when more than 2% of the current mode's prompts timed
  out.
- **Mode:** back to `shadow` when the filter no longer beats memware's own picks by 5 points of
  precision and finds no more relevant facts. At that point the filter isn't paying for its
  latency or for sending prompts off the machine.

**A6. Clean up.** Run `python3 garden.py clean --work <work>`. The batches hold prompt text, and
the verdicts no longer need them.

**Out of a setting's reach.** Some findings can't be fixed with `memware config`. Report them as
issues rather than editing an installed plugin:
- **The subject rule's constants:** `RARE_SHARE` and `RARITY_MIN_PASSAGES` in
  `memware/index.py`. Subject-rule offenders that all share one common word argue for a
  lower rare-word share.
- **derive's model:** `derive.model`. Many offenders that derive filed as trivia argue for a
  stronger model, or a stricter prompt.

## Part B: tend the ledger

Build one proposal table from B1–B4, show it, and apply only the rows the user approves.

**B1. Offenders.** Take the belief ids in the report's `offenders_injected` (beliefs shown at
least 5 times and at least 80% noise) and the top of `offenders_subject_rule`. For each id, run
`memware beliefs --explain ID` and decide which of these it is:

- **Vague subject.** A true fact filed under a subject that matches too much, such as a single
  common word (`skills`, `card`) or a subject that repeats its own value. Propose re-asserting it
  under the specific subject a person would name when they need it:
  ```bash
  memware assert "<specific subject>" "<relation>" "<value>" --valid-from <its valid_from> --source "<its source>"
  memware beliefs retract <old id> --apply
  ```
  Keeping the original `--source` session pointer leaves it retractable if that session is later
  pruned, and keeping `--valid-from` keeps its date honest in the injected line.
- **Trivia or obsolete.** It's true but nobody will need it, or it no longer holds. Propose
  `memware beliefs retract <id> --apply`.
- **A reading the staleness gate missed.** A count, a status, or a "current" pointer that
  `--explain` calls `durable` (`open card count | 305`, `current pr | PR #18`, `state | dirty and
  8 behind`). Propose retracting it. Separately, list the triple as a classifier miss for the
  memware maintainers: a new case for `tests/data/volatility_cases.jsonl`. A triple is the user's
  data, so ask before it goes into a public issue.
- **Duplicate.** The same fact under several keys (`kanban review | report path`, `kanban audit |
  report location`). Keep the key a person would name and retract the others.
- **Mislabeled.** It really is relevant where it appears. Keep it and note that.

**B2. Stale and orphaned.** Run `memware beliefs --stale`,
`memware beliefs retract --stale` (a dry run) and `memware beliefs retract --orphaned` (a dry
run). Injection already leaves stale beliefs out, and retracting them keeps recall clean too.
Propose the ones that are clearly spent.

**B3. Contested.** Run `memware review list`. Summarise each pending supersession and ask the user
to approve or reject it (`memware review approve|reject <id>`).

**B4. Whole-ledger pass.** Do this every fourth cycle, or when `memware stats` shows current
beliefs up 20% since the last pass. Read `memware --json beliefs` and look for:

- **Duplicates:** the same fact under two keys. Keep the better-named key and retract the other.
- **Contradictions:** two current beliefs that can't both be true. Ask the user which holds.
- **Subjects that will misfire:** one common word, a sentence, a path with no name, or the value
  repeated in the subject.
- **Instructions posing as facts:** beliefs that order the agent to do something. The gate
  already keeps these out of injection. Retract them if they came from derive.

Budget: flag at most 30 rows, most harmful first. The ones injected most often come first.

## Finish

Summarise for the user in a few lines:
- the precision of memware alone against the filter, and the trend;
- what changed: the setting, and the beliefs retracted or re-asserted;
- what waits on them: proposals they declined or haven't decided;
- when the next cycle is due.
