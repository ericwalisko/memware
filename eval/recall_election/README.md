# Recall election eval

Does the model *elect* to call memware's `recall` tool when it should, and leave it alone when
it should not? The variable under test is the `recall` tool description (one `.md` per
variant). Everything else is held fixed: a stub MCP server named `memware` that returns canned
hits, a fixture project as the working tree, a read-only built-in toolset, and a scenario set
with positives (the answer lives in past sessions) and negatives (the answer is in the tree).

Nothing here is part of the package. Run from the repo root with the `mcp` and `dev` extras.

| file | role |
|---|---|
| `stub_server.py` | MCP stdio server `memware` with `recall`, `read_session`, `beliefs`; recall's description comes from `RECALL_DESCRIPTION_FILE`; every call is appended to `RECALL_CALL_LOG` |
| `run.py` | runs the (variant x scenario x model x repeat) grid through `claude -p`, one JSON row per cell, resumable |
| `probe.py` | two isolation probes that must PASS before any run |
| `score.py` | TPR / FPR / balanced accuracy with Wilson intervals, per-scenario signal check, `report.md` |
| `test_harness.py` | unit tests (parser on a real captured stream, fatal detection, loaders, scoring) |
| `scenarios.json`, `scenarios.md` | the scenario set and its rationale |
| `fixture_project/` | the working tree every cell runs in |
| `variants/` | one `<id>.md` per recall description; the stem is the variant id. `control` is the docstring in `src/memware/mcp_server.py` (PR #15), `baseline` the pre-#15 how-only text, the other six are authored candidates |
| `gate_variants/` | `never_call` and `must_call`, the proof-of-red pair; never part of the scored grid |

## How to run

```bash
cd <repo root>
# 0. harness self-test (no model calls)
uv run --extra dev pytest eval/recall_election/test_harness.py -q

# 1. probe isolation, once per model you will run
uv run --extra mcp --extra dev python eval/recall_election/probe.py \
    --variant eval/recall_election/variants/baseline.md --model opus

# 2. the grid (resumable: rerun the same command to continue; --limit N for a smoke run)
uv run --extra mcp --extra dev python eval/recall_election/run.py \
    --variants eval/recall_election/variants --scenarios eval/recall_election/scenarios.json \
    --models sonnet opus --repeats 3 --parallel 4 --out eval/recall_election/results.jsonl

# 3. score (writes report.md next to the results file and prints it)
uv run python eval/recall_election/score.py eval/recall_election/results.jsonl
```

`run.py --dry-run` lists the pending cells and the time estimate without spawning anything.
`--raw-dir DIR` keeps each cell's raw stream-json and stderr for debugging.

Scenario file: a list (or `{"scenarios": [...]}`) of objects with `id`, `prompt`, `class`
(free text, reported per class) and `expect` (`true` when recall should be called; `false`,
`"no"` and `"negative"` are also accepted).

## Isolation invariants

Every cell is `claude -p` with exactly this shape (`run.build_argv`; `probe.py` reuses it so
the probes test what runs):

```
claude -p <prompt> --model <model>
    --mcp-config <tmp>/mcp.json --strict-mcp-config
    --no-session-persistence --output-format stream-json --verbose --max-turns 12
    --allowedTools mcp__memware__recall mcp__memware__read_session mcp__memware__beliefs Grep Read Glob
    --tools Grep,Read,Glob
    --setting-sources project            # --isolation setting-sources (default)
    [--settings '{"disableAllHooks": true}']   # --isolation settings, or both
    --append-system-prompt "[memware-eval]"
```

cwd = the fixture project, stdin closed, env = `os.environ` minus `ANTHROPIC_API_KEY` plus
`MEMWARE_NO_CAPTURE=1`.

| flag / choice | why |
|---|---|
| `--mcp-config` + `--strict-mcp-config` | only the stub server exists. The real `memware-mcp` is registered at user scope (`claude mcp list`) and the memware plugin ships one too; strict mode drops both. The stub is named `memware` so the tools carry their production names (`mcp__memware__recall`), which is what the description is being tuned for |
| `--setting-sources project` | user settings are not loaded: no user hooks (the memware plugin's `UserPromptSubmit` hook injects a "Known facts" block that would change the prompt), no `enabledPlugins`, no default model. The fixture's own project settings, if any, still apply and count as part of the fixture |
| `--settings '{"disableAllHooks": true}'` | the fallback if setting-sources ever fails the probe (`--isolation settings`); `--isolation both` sends both. Not needed as of 2026-09-10 (see the probe record below) |
| never `--bare` | it skips the subscription login (OAuth and keychain are never read) and would need an API key |
| `ANTHROPIC_API_KEY` removed | subscription login, as `memware.derive.ClaudeCodeProvider` does; the run must not bill a key that happens to be in the shell |
| `MEMWARE_NO_CAPTURE=1` | if a memware hook did somehow run, it skips capture (docs/eval.md) |
| `--no-session-persistence` | no transcript under `~/.claude/projects`, so the eval cannot become evidence for the next sync (docs/eval.md, item 7) |
| `--append-system-prompt "[memware-eval]"` | `memware.eval.MARKER`; sync skips any transcript carrying it and `memware prune --containing '[memware-eval]'` removes stragglers. Added only when `claude --help` lists the flag (2.1.268 does); belt and braces with the line above. `--no-marker` drops it |
| `--tools Grep,Read,Glob` | the built-in set is cut to read-only search so the model's choice is recall vs. looking in the tree, with no third path (Bash, `git log`, the web). MCP tools are not part of the built-in set and survive; the init event lists exactly six tools |
| `--allowedTools ...` | pre-approves those six so a headless run never blocks on a permission prompt; `permission_denials` is recorded per row and should be 0 |
| `--max-turns 12` | opus runs 4-9 tool rounds on these prompts (recall, then Grep/Read to confirm); 3 cut most cells off with `error_max_turns`. Cells that still hit the cap score as invalid |
| `--output-format stream-json --verbose` | the transcript with `tool_use` blocks; the init event carries the tool list the probes check |
| cwd = fixture project | in-tree questions must be answerable by Grep/Read for the negatives to be fair. Negatives that ask for an edit or a test run are answered as far as read-only tools allow; what is scored is whether recall was elected, not whether the task got done |
| stdin closed | otherwise `claude` waits on piped stdin |
| one stub per cell, own call log | `recall_called` (from the transcript) is cross-checked against what the server saw; `recall_mismatch` should be 0 everywhere |

### Probe record

`probe.py --isolation setting-sources --model opus`, claude 2.1.268, 2026-09-10: **PASS / PASS**.
Probe 1 listed exactly `Glob, Grep, Read, mcp__memware__beliefs, mcp__memware__read_session,
mcp__memware__recall`, matching the init event; no other `mcp__` name. Probe 2 answered `NO`.
The `--settings` fallback was therefore not exercised; `--isolation setting-sources` is the
default. Re-run the probe after any Claude Code upgrade or settings change.

Re-probed 2026-09-11 with `variants/control.md`: PASS / PASS again (3.9 s and 4.0 s).

### Red and smoke record

2026-09-11, opus, one repeat. Proof of red on `past_decision_1`, `rationale_1`,
`not_in_tree_1` / `find_code_1`, `explain_file_1`, `refactor_1`: `never_call` elected on
0 of 3 positives (it went to `beliefs` or Grep instead), `must_call` on 3 of 3 negatives;
12/12 valid, 0 mismatches, 0 permission denials, 13-48 s per cell (mean 24.8 s). Smoke with
`control` and `baseline` on `rejected_alternative_1`, `earlier_session_1` / `find_code_2`,
`style_question_1`: 8/8 valid, 0 mismatches, mean 21.5 s per cell; control TPR 1.00 FPR 0.00,
baseline TPR 1.00 FPR 0.50 (it elected on `style_question_1`). `memware stats` grew only from
unrelated live sessions during the runs; no turn in the store has a fixture-project source.

## Gate sequence

1. **Probe.** `probe.py` on every model in the run. Both probes must PASS; a FAIL means the
   real memware server or its hook leaked in and every number downstream is contaminated.
   If probe 1 fails on `setting-sources`, retry with `--isolation settings`, then `both`, and
   run the grid with whichever passed.
2. **Proof of red.** `--variants eval/recall_election/gate_variants` on 3 positives and 3
   negatives, one model, one repeat. `never_call` ("Deprecated. Never call this tool") must
   elect on at most 1 of the 3 positives and `must_call` ("You MUST call this tool first on
   every request") on at least 2 of the 3 negatives. Otherwise the description is not what is
   being measured (check the probe, the stub's description file, the scenario prompts) and no
   other result counts. The gate variants never go in the scored grid.
3. **Smoke.** `--limit 8 --repeats 1` on one model. Every row should have `valid: true`,
   `recall_mismatch: false`, `permission_denials: 0`, `elapsed_s` around 3-15.
4. **Full.** All variants x scenarios x models x repeats, then `score.py`. Read balanced
   accuracy first, then the intervals: with n positives per (variant, model) a difference
   inside overlapping Wilson intervals is not a result. Check the per-scenario table for
   `no signal` rows and replace those scenarios before the next round.

## Cost per full run

cells = variants x scenarios x models x repeats. Measured: 5.4 s for a sonnet cell that
called recall once and answered; 13-48 s (mean 21-25 s) per opus cell on the real scenarios,
where the model follows recall with several Grep/Read rounds; 4 s per opus probe. Wall time
is about `cells x 25 s / --parallel` for opus. Example: 8 variants x 24 scenarios x 2 models
x 3 repeats = 1152 cells, ~1.5 h at `--parallel 4` if half are opus. Each cell is one short `claude -p` conversation on the
subscription; there is no per-token bill. A usage-limit or auth error in any cell stops the
whole run (exit 2, nothing recorded for that cell) rather than writing a hundred failures;
rerun the same command to resume. Only stderr is scanned for that on a successful cell; the
model's answer is scanned on failed cells alone, because the fixture is an auth gateway and
answers routinely say "unauthorized" or "401". Streams carry `rate_limit_event` lines with
`status: allowed_warning` near the limit; those are informational and not treated as a stop.

## The `[memware-eval]` marker

From docs/eval.md: `[memware-eval]` (`memware.eval.MARKER`) in every evaluation prompt lets
`memware-eval --corpus` rebuild a clean store that skips any transcript carrying it, and
`memware prune --containing '[memware-eval]'` removes stragglers from a live store. This harness
sends it through `--append-system-prompt "[memware-eval]"` whenever `claude --help` shows that
flag (it does in 2.1.268), so the marker reaches the transcript without editing the scenario
prompts. With `--no-session-persistence` no transcript is written in the first place; the
marker covers the case where that ever changes, and `MEMWARE_NO_CAPTURE=1` covers a hook that
ever runs. Seed `~/.memware/ignore-markers.txt` with the marker too if you keep raw streams
(`--raw-dir`) anywhere a sync might index.

## Result rows

One JSON line per cell:

```
variant, scenario, class, expect, model, repeat,
tools_called (ordered), first_tool, recall_called, recall_logged, recall_mismatch, stub_calls,
recall_queries (first recall call), n_phrasings, turns, permission_denials, elapsed_s,
final_text (300 chars), rc, error, valid, isolation, ts
```

`valid` is false for timeouts, non-zero exits and `is_error` results; `score.py` excludes those
and counts them (docs/eval.md, item 8: mark invalid answers, do not score them).
