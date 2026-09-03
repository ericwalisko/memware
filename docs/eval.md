# Evaluation

Two levels. The first needs no model and runs in CI; the second is how you
decide whether memware earns its place in your own setup.

## Retrieval-level (`memware-eval`)

A question set is JSONL with `expect_any`, `not_expect` and `type`
(`fact` / `stale` / `negative`). The runner retrieves top-k beliefs and turns
and scores containment. See `eval/questions.example.jsonl`.

```bash
memware-eval eval/questions.example.jsonl --db ./demo.db
```

Reported: accuracy, stale rate (a superseded value appearing in context),
per-type accuracy, median latency, and the size of the retrieved context.
Recall quotes matching passages rather than whole turns, so that context is
what a prompt would actually receive.

Building a question set from your own transcripts: pick facts that an agent
stated at time T, and for stale questions pick facts whose value changed
between T1 and T2 — the question must expect the T2 value and forbid the T1
value. Keep the set private; only commit synthetic examples.

### Freeze the corpus before you compare

If the index keeps syncing while you work, the eval measures your working
session. Running one question set against a live store during development of
the passage index moved the *unchanged* whole-turn baseline between 17/24 and
24/24 on fact questions, in both directions, within three hours. 847 turns
written that day were doing both halves of it: they answered questions the set
had been built to ask, and — being the most recent thing in the index, which
recency weighting rewards — they crowded the real evidence out of the top 8.

So snapshot the database and point every arm of the comparison at the same
file: copy the store, stop syncing it, and drop turns newer than the question
set (`DELETE FROM turn WHERE ts >= '<date the set was frozen>'` — passages
cascade). A before/after measured on two different corpora is not a
measurement. This is the retrieval-level cousin of point 6 below.

## End-to-end protocol (agent + memware vs agent alone)

1. **Turn off every other memory system** for the duration of the run, on
   both sides of the comparison. Mixed memory sources make results
   uninterpretable.
2. Conditions, run on the same question set with the same model and prompt:
   - `alone`: the agent with whatever built-in memory it ships (instruction
     files, its own notes).
   - `alone + memware`: the same, plus the hook bundle (session sync, prompt-time
     belief context) and the MCP tools.
3. Score each answer by containment of the expected value, absence of the
   stale value, and abstention on negatives. Record tokens injected per prompt
   and wall-clock latency added by the hooks.
4. Report per type. A memory system that raises `fact` accuracy but also
   raises the stale rate has not helped.
5. **Restrict the "alone" arm to its memory surface.** A tool-using agent
   left with file search, a terminal or the web will answer from the corpus
   on disk or from the internet, not from memory — in one run an agent
   answered 92% of fact questions by grepping transcripts (93 file-search
   calls for 36 questions). Enable only the agent's own memory and
   session-recall tools for the comparison, and record the toolset in every
   result row.
6. **The agent's own session memory learns from the eval.** Every eval
   question becomes a session; an agent with session recall will answer the
   second run from the first run's answers (in one case 179 of 251 recalled
   sessions were earlier eval sessions). Point the "alone" arm at an empty
   session store — and check that a "profile" or "workspace" actually gets its
   own database rather than sharing the default one — or restrict it to its
   durable memory files only.
7. **Mark invalid answers, don't score them.** Quota and rate-limit text
   ("you've hit your session limit"), empty answers and timeouts are not
   answers; exclude them from the summary and report the count.
8. Treat a change to production memory as a user-facing, data-affecting change:
   have a human review the numbers before cutting over.

Public long-horizon benchmarks (LongMemEval, LoCoMo) can be adapted by loading
their conversation histories through the generic ingest and asking their
questions through `memware-eval`; adapters are welcome as contributions.
