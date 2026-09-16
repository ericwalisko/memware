# Isolated rerun of the recall election — STOPPED AT PROBE 3

Date: 2026-09-15. Harness: `eval/recall_election` at commit `8e5ccbd`.

**Verdict: KEEP `control`.** The grid was never run, so the decision rule has no data to
apply. Under the rule as written — ship `synthesized` only if it beats `control` at p < 0.05
on *each* model with no loss of positive election rate — the absence of a result is a keep,
not a defer: `score.py`'s `decision_section` prints exactly that when either variant is
missing from the results.

## Why it stopped

The card's escalation clause: *"If probe 3 cannot pass, stop and report before the full
run."* Probe 3 fails on its first leg.

```
$ python3 probe.py --variant variants/synthesized.md --model opus
probe 1 (tool list): PASS
  memware tools listed: ['beliefs', 'read_session', 'recall']
  other mcp__ listed:   []
  init event tools:     ['Glob', 'Grep', 'Read', 'mcp__memware__beliefs',
                         'mcp__memware__read_session', 'mcp__memware__recall'] -> ok
  mcp servers (init):   [{'name': 'memware', 'status': 'connected'}]
probe 2 (Known facts): PASS
  first word: 'NO'
probe 3 (no memory / not self-aware): FAIL
  init-event memory paths: ['memory_paths={"auto": "/Users/ericwalisko/.claude/projects/
      -private-var-folders-21-...-T-wkspc-pa9g-upr-ga...']
  cwd: /private/var/folders/21/.../T/wkspc-pa9g_upr/gateway -> neutral
  eval words in answer:    [] (need none)
  answer: The project directory is `gateway` ... without reading any of its files I can't
          say what it's for — the name suggests some kind of gateway/proxy service, but
          that's a guess rather than something I know.
RESULT: FAIL     elapsed 2.87 / 2.43 / 3.69 s
```

Probe 3 has two legs and they split:

- **3a, auto-memory path — FAIL.** The `system/init` event carries a `memory_paths` key, and
  its `auto` entry is a real path: `~/.claude/projects/<slugified cwd>/memory`. The card's
  rule is literal — *fail if the init event reports any auto-memory path* — and it does.
- **3b, self-awareness — PASS.** The cwd is neutral (`.../wkspc-<rand>/gateway`, no "eval",
  "fixture" or "recall" anywhere in it), and the model, asked what project it is in, named
  only `gateway` and said outright it could not tell what the codebase was for. No eval,
  stub or fixture word appears in the answer.

## What the failure is, and what it is not

The path `memory_paths.auto` points at the auto-memory directory *for this cell's cwd*. The
cwd is a temp copy that has never existed before, so the directory it names does not exist and
nothing was loaded from it — leg 3b is the behavioural check on that, and it passes. So this
is very likely a **reported pointer, not loaded content**.

That is a judgment call, and the card did not delegate it: the rule says any reported path
fails. Two things follow, and only Eric can pick:

1. **The rule stands.** Then the harness needs `HOME` (or whatever sets `memory_paths.auto`)
   redirected per cell so the init event reports nothing, and the grid runs after that. The
   risk is that redirecting `HOME` on macOS also moves the subscription credentials the run
   depends on; that needs testing, not assuming.
2. **The rule is narrowed** to "reports an auto-memory path that exists and is non-empty",
   which is what it was plainly written to catch. Probe 3a then passes as the code stands,
   and the grid can run unchanged.

Either way the honest state today is: **probes 1 and 2 pass, probe 3 fails, no grid data
exists, keep `control`.**

## What is in place and unexercised

- Per-cell isolation: fresh `copytree` of the fixture into `mkdtemp(prefix="wkspc-")` under a
  directory named `gateway`, asserted outside any git repo and asserted free of the words
  eval/fixture/recall, deleted when the cell ends. The stub's mcp-config dir uses the same
  neutral prefix (it was `recall-election-`, which the model could read off `--mcp-config`).
- Per-row `tool_inputs` (Grep/Glob `path` + `pattern`, Read `file_path`) and `out_of_copy`;
  any absolute or `..`-climbing path that resolves outside the cell's copy invalidates it.
- `~/.claude/projects/<cwd slug>` swept after every cell: removed only once verified to hold
  no `*.jsonl`; a transcript found there is recorded on the row and invalidates the cell.
- `recall_mismatch` (transcript vs stub log) was already recorded per row and is surfaced in
  the report header; unchanged.
- Scenarios: 12 positives / 12 negatives, the 13 no-signal ones replaced and the five action
  negatives reworded read-only. Unrun, so their signal is unmeasured — the point of the
  replacement is exactly that it has to be re-measured.
- `variants/synthesized.md`, and the decision rule in `score.py`.

## Contamination check

Read-only, counts only, against the live `~/.memware/memware.db` opened `mode=ro`, by
**source attribution** (never turn counts):

```
belief.source: wkspc-sourced=0 recall_election-sourced=0
turn.source:   wkspc-sourced=0 recall_election-sourced=0
cursor.source: wkspc-sourced=0 recall_election-sourced=0
```

No turn or belief in the store has a source under any `wkspc-` temp copy or under
`recall_election`. A `Glob` for `*wkspc*` under `~/.claude/projects` returned nothing, which
is consistent with the per-cell sweep having removed the three probe cells' project
directories after verifying each held no `*.jsonl` — but note this was three probe cells, not
a grid, so the sweep is only lightly exercised.

## Caveat on `synthesized.md`

`gh` is not permitted in this sandbox, so **PR #21's body could not be read** and the
candidate text could not be copied from it. `variants/synthesized.md` is a reconstruction
from the two variants the 2026-09-10 report shows winning on opus — `grep-contrast`'s
grep-vs-recall contrast paragraph and `terse-triggers`' trigger list — plus `control`'s
parameter documentation. **A human must diff it against PR #21 before any grid run counts as
testing the candidate.** If it differs, the file is the thing to fix, not the harness.
