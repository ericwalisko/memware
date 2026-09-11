# Recall election eval

Does the model *elect* to call memware's `recall` tool when it should, and leave it alone when
it should not? The variable under test is the `recall` tool description (one `.md` per
variant). Everything else is held fixed: a stub MCP server named `memware` that returns canned
hits, a fresh copy of a fixture project as the working tree, a read-only built-in toolset, and
a scenario set with positives (the answer lives in past sessions) and negatives (the answer is
in the tree).

Nothing here is part of the package. Run from the repo root with the `mcp` and `dev` extras.

| file | role |
|---|---|
| `stub_server.py` | MCP stdio server `memware` with `recall`, `read_session`, `beliefs`; recall's description comes from `RECALL_DESCRIPTION_FILE`; every call is appended to `RECALL_CALL_LOG` |
| `run.py` | runs the shuffled (variant x scenario x model x repeat) grid through `claude -p`, each cell in its own copy of the fixture, one JSON row per cell, resumable |
| `probe.py` | three isolation probes per model that must PASS before any run |
| `score.py` | TPR / FPR / balanced accuracy with Wilson intervals, per-scenario signal check, paired sign test against control, the ship/keep decision, `report.md` |
| `test_harness.py` | unit tests (parser on a real captured stream, isolation and out-of-copy checks, count mismatch, shuffle, fatal detection, loaders, scoring, sign test, decision) |
| `scenarios.json`, `scenarios.md` | the scenario set and its rationale |
| `fixture_project/` | the tree copied into every cell's working directory |
| `variants/` | one `<id>.md` per recall description; the stem is the variant id. `control` is the docstring in `src/memware/mcp_server.py` (PR #15), `synthesized` the grep-contrast/terse-triggers synthesis the decision may ship, `baseline` the pre-#15 how-only text, the rest authored candidates |
| `gate_variants/` | `never_call` and `must_call`, the proof-of-red pair; never part of the scored grid |

## How to run

```bash
cd <repo root>
# 0. harness self-test (no model calls)
uv run --extra dev pytest eval/recall_election/test_harness.py -q

# 1. probe isolation on every model in the run
uv run --extra mcp --extra dev python eval/recall_election/probe.py \
    --variant eval/recall_election/variants/control.md --models opus sonnet

# 2. the grid: 5 variants x 24 scenarios x 2 models x 5 repeats = 1200 cells, shuffled
#    (resumable: rerun the same command to continue)
uv run --extra mcp --extra dev python eval/recall_election/run.py \
    --variant-ids synthesized,grep-contrast,terse-triggers,control,memory-persona \
    --models sonnet opus --repeats 5 --parallel 4 --out eval/recall_election/results.jsonl

# 3. score (writes report.md next to the results file and prints it; the last line is the verdict)
uv run python eval/recall_election/score.py eval/recall_election/results.jsonl
```

`run.py` flags: `--variants DIR` (default `variants/`) and `--variant-ids a,b,c` choose the
descriptions; `--scenario-ids` narrows the scenarios; `--seed` (default 20260911) fixes the
shuffle; `--limit N` runs the first N pending cells of the shuffled order; `--dry-run` lists
the pending cells and the time estimate without spawning anything; `--raw-dir DIR` keeps each
cell's raw stream-json and stderr (put it under `/private/tmp`, not anywhere a memware sync
indexes); `--marker` sends the `[memware-eval]` line (off by default, see below).

Scenario file: a list (or `{"scenarios": [...]}`) of objects with `id`, `prompt`, `class`
(free text, reported per class) and `expect` (`true` when recall should be called; `false`,
`"no"` and `"negative"` are also accepted).

## Isolation invariants

PR #21's adversarial review found the first run contaminated in ways its gates did not catch:
every cell loaded the checkout's Claude Code auto-memory, the cwd path, branch name and commit
subject named the eval (18 sonnet query sets mention "recall election", "stub" or
"fixture_project"), Grep/Read/Glob reached files outside the fixture with no record of which,
and variants ran in blocks. Every cell now has this shape (`run.run_claude`; `probe.py` calls
the same function, so the probes test what runs):

```
work root  /private/tmp/gateway-work                 git -C <root> rev-parse must fail
copy       /private/tmp/gateway-work/gateway-XXXXXXXX/   the fixture's contents; the cwd
meta       /private/tmp/gateway-work/gwmeta-XXXXXXXX/    mcp.json, calls.jsonl
           both deleted when the cell ends

claude -p <prompt> --model <model>
    --mcp-config <meta>/mcp.json --strict-mcp-config
    --no-session-persistence --output-format stream-json --verbose --max-turns 12
    --allowedTools mcp__memware__recall mcp__memware__read_session mcp__memware__beliefs Grep Read Glob
    --tools Grep,Read,Glob
    --setting-sources project                   # --isolation setting-sources (default)
    [--settings '{"disableAllHooks": true}']    # --isolation settings, or both
    [--append-system-prompt "[memware-eval]"]   # only with --marker
```

stdin closed; env = `os.environ` minus `ANTHROPIC_API_KEY`, plus `MEMWARE_NO_CAPTURE=1` and
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.

| invariant | why |
|---|---|
| cwd is a fresh copy of `fixture_project`'s contents, one per cell | In place, the fixture sat inside the memware checkout: a worktree resolves to the main checkout's Claude Code project, so its auto-memory loaded, and the cwd path itself said `eval/recall_election/fixture_project`. A copy also means no cell sees what an earlier one left behind |
| the work root is outside any git repository, checked before the first cell (`git -C /private/tmp/gateway-work rev-parse` must fail) | inside a repository Claude Code puts the branch, status and recent commit subjects in the system prompt; in the first run those named the eval |
| MCP config and stub call log live in a sibling `gwmeta-` directory, never inside the copy | a Glob or Grep over the cwd would otherwise find `mcp.json` (it names the stub server and the variant file) and `calls.jsonl` (the model's own recall calls). The sibling is reachable only through a path that already invalidates the cell |
| no path the model can see contains `eval`, `fixture`, `recall`, `stub` or `harness` | a path is a label. The root and the prefixes carry none, a random `mkdtemp` suffix that spells one is redrawn, and the preflight and every copy check the names in the tree. `mcp.json` still names the interpreter and `stub_server.py` inside the checkout: claude reads it, the model is never shown it |
| `__pycache__`, `*.pyc`, `.git`, `.venv` and tool caches are not copied | bytecode embeds the absolute source path it was compiled from, which would put `.../eval/recall_election/fixture_project/...` within Grep's reach |
| no `CLAUDE.md`, `CLAUDE.local.md`, `.claude` or `.mcp.json` in the work root or any ancestor (preflight) | Claude Code loads these from the cwd upward; one dropped in world-writable `/private/tmp` would reach every cell |
| both directories are deleted after the cell; the row records `copy_dir` and `dirs_removed` | nothing accumulates over 1200 cells, the row says which copy produced it, and a cell that reads a concurrent cell's copy is already `out_of_copy` |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | auto-memory (the feature that loads a project's `MEMORY.md` and tells the model about its memory directory, and the first run's leak) is on by default even in a fresh directory: the init event carried `memory_paths.auto` (`~/.claude/projects/-private-tmp-gateway-work-gateway-.../memory/`) and each spawn left an empty project directory behind. With the variable set the field is absent and no project directory is created (observed 2026-09-11, claude 2.1.268). Probe 3a fails if it comes back |
| every tool input is recorded; a cell is invalid (`out_of_copy`) if an absolute path input does not resolve inside its copy or a relative one climbs out through `..` | Grep, Read and Glob are not confined to the cwd; at least 3 first-run answers used files outside the fixture and the rows could not say which. Checked: `path` and `file_path` on every tool, `glob` on Grep, `pattern` on Glob (Grep's `pattern` is a regex). A glob is judged by its literal prefix; a `..` after a wildcard counts as out, since `**` may match nothing. `~` is expanded, symlinks such as `/tmp` are resolved |
| no `[memware-eval]` marker unless `--marker` | it rides in the system prompt, where it names the eval to the model; `--no-session-persistence` leaves no transcript for it to mark |
| `--mcp-config` + `--strict-mcp-config` | only the stub server exists. The real `memware-mcp` is registered at user scope (`claude mcp list`) and the memware plugin ships one too; strict mode drops both. The stub is named `memware` so the tools carry their production names (`mcp__memware__recall`), which is what the description is being tuned for |
| `--setting-sources project` | user settings are not loaded: no user hooks (the memware plugin's `UserPromptSubmit` hook injects a "Known facts" block that would change the prompt), no `enabledPlugins`, none of the user's own skills (absent from the init event's list), no default model. The copy has no project settings |
| `--settings '{"disableAllHooks": true}'` | the fallback if setting-sources ever fails the probe (`--isolation settings`); `--isolation both` sends both. Not needed as of 2026-09-11 |
| never `--bare` | it skips the subscription login (OAuth and keychain are never read) and would need an API key |
| `ANTHROPIC_API_KEY` removed | subscription login, as `memware.derive.ClaudeCodeProvider` does; the run must not bill a key that happens to be in the shell |
| `MEMWARE_NO_CAPTURE=1` | if a memware hook did somehow run, it skips capture (docs/eval.md) |
| `--no-session-persistence` | no transcript under `~/.claude/projects`, so the eval cannot become evidence for the next sync (docs/eval.md, item 7) |
| `--tools Grep,Read,Glob` | the built-in set is cut to read-only search so the model's choice is recall vs. looking in the tree, with no third path (Bash, `git log`, the web). MCP tools are not part of the built-in set and survive; the init event lists exactly six tools |
| `--allowedTools ...` | pre-approves those six so a headless run never blocks on a permission prompt; `permission_denials` is recorded per row and should be 0 |
| `--max-turns 12` | opus runs 4-9 tool rounds on these prompts (recall, then Grep/Read to confirm); 3 cut most cells off with `error_max_turns`. Cells that still hit the cap score as invalid |
| `--output-format stream-json --verbose` | the transcript with `tool_use` blocks; the init event carries the tool list and memory fields the probes check |
| stdin closed | otherwise `claude` waits on piped stdin |
| one stub per cell, own call log, checked for presence and count | `recall_mismatch` compares whether recall ran in the transcript and in the stub's log; `count_mismatch` compares how many times. Presence alone missed a schema-rejected call in the first run (two calls in the transcript, one reached the stub). Both should be 0 |
| the whole grid is shuffled with `random.Random(--seed)`, default 20260911 | in blocks, anything that drifts during a 1.6 h run (usage throttling, a model-side change) lands on whole variants. Cells are sorted by key before the shuffle, so the order depends only on the grid and the seed and a resumed run keeps it |

## Probes

`probe.py` runs three `claude -p` spawns per model with the cell isolation above. All must PASS
on every model before the grid; a FAIL means something outside the variant reaches the model
and every number downstream is contaminated.

1. **Tool list.** The model lists exactly `recall`, `read_session` and `beliefs` under
   `mcp__memware__` and no other `mcp__` name; the init event's tool list agrees. Fails if the
   real memware server or any other MCP server leaks in.
2. **Known facts.** Asked whether its context holds a block starting "Known facts" (what the
   memware `UserPromptSubmit` hook injects), the model answers NO.
3. **Context.** (a) Every init event of the model's three spawns exists and carries no
   non-empty memory path field (`memory_paths.auto` or `.team`, or any other key naming memory).
   (b) Asked "In one sentence, describe the project you are working in.", the answer contains
   none of eval, stub, fixture, harness, scenario, benchmark (case-insensitive substrings).
   Probes 1 and 2 were too narrow to see the auto-memory leak or the harness names; this one
   looks for both.

Each probe prints the copy it ran in, the init event's cwd, whether both directories were
removed, and any tool input outside the copy.

### Probe record

2026-09-11, claude 2.1.268, `--isolation setting-sources`, `variants/control.md`, opus:
**PASS / PASS / PASS** (2.3 s, 3.7 s, 7.8 s). Probe 1 listed exactly `Glob, Grep, Read,
mcp__memware__beliefs, mcp__memware__read_session, mcp__memware__recall`, matching the init
event. Probe 2 answered `NO`. Probe 3: no memory fields in any init event; after Glob and Read
inside its copy the answer was "This is `gateway`, a stdlib-only Python edge gateway (v0.3.1)
that checks each client request's signature, then forwards job submissions and status lookups
to an internal job runner and passes the runner's response back." Every spawn's init cwd was
its copy, both directories were gone afterwards, no input left the copy. Proof of red for 3a:
the same spawn without `CLAUDE_CODE_DISABLE_AUTO_MEMORY` (stopped right after the init event,
before any model call) carried `memory_paths.auto`. Sonnet not yet probed with this harness.
Re-run the probes after any Claude Code upgrade or settings change.

One isolated opus cell, same day (`control` on `rationale_1`): valid, 22.9 s, init cwd
`/private/tmp/gateway-work/gateway-7qw08shx` = `copy_dir`, the copy held the fixture's files
and the sibling `gwmeta-` held `mcp.json` then `calls.jsonl`; both were gone when the cell
ended. Seven tool inputs recorded (three Reads under the copy, two Greps, two recalls),
`out_of_copy` empty, recall 2 in the transcript and 2 at the stub, `memory_paths` `{}`, no
new `~/.claude/projects` entry.

### Earlier records (PR #21 harness: fixture used in place, marker sent)

Probe 1 and 2 PASS on opus 2026-09-10 and 2026-09-11. Proof of red 2026-09-11, opus, one
repeat, `past_decision_1`, `rationale_1`, `not_in_tree_1` / `find_code_1`, `explain_file_1`,
`refactor_1`: `never_call` elected on 0 of 3 positives, `must_call` on 3 of 3 negatives; 12/12
valid, 0 mismatches, 13-48 s per cell. The full 384-cell run (`results/2026-09-10/`) is not
decisive for the contamination described above.

## Gate sequence

1. **Probe.** `probe.py` on every model in the run; all three probes PASS. If probe 1 fails on
   `setting-sources`, retry with `--isolation settings`, then `both`, and run the grid with
   whichever passed.
2. **Proof of red.** `--variants eval/recall_election/gate_variants --scenario-ids <3 positive
   ids>,<3 negative ids>`, one model, one repeat. `never_call` ("Deprecated. Never call this
   tool") must elect on at most 1 of the 3 positives and `must_call` ("You MUST call this tool
   first on every request") on at least 2 of the 3 negatives. Otherwise the description is not
   what is being measured (check the probe, the stub's description file, the scenario prompts)
   and no other result counts. The gate variants never go in the scored grid.
3. **Smoke.** `--limit 8 --repeats 1` on one model. Every row should have `valid: true`,
   `out_of_copy: []`, `recall_mismatch` and `count_mismatch` false, `permission_denials: 0`,
   `dirs_removed: true`, `memory_paths: {}`.
4. **Full.** The 1200-cell grid above, then `score.py`. Check the header counts (invalid by
   reason, mismatches, memory paths), the per-scenario table for `no signal` rows, then the
   Decision section.

## Decision rule

`score.py` ends with `VERDICT: SHIP synthesized` or `VERDICT: KEEP control`. SHIP only if, on
**each** model separately:

1. **Synthesized beats control on a paired sign test.** Match cells on (scenario, repeat)
   where both the synthesized and the control cell are valid. A cell is correct when
   `recall_called == expect`. Count the discordant pairs: b = synthesized correct and control
   wrong, c = the reverse. With n = b + c, the exact two-sided binomial
   p = min(1, 2 x sum over i <= min(b, c) of C(n, i) / 2^n). Require b > c and p < 0.05.
2. **No drop in positives.** Over the same matched positive pairs, synthesized elects recall at
   least as often as control.

Otherwise KEEP control. Per model, because one description ships to every model and a win on
one must not hide a loss on the other; paired, because which scenario a cell ran moves the
election far more than the description does (in the first run 10 of 12 positives were elected
by every copy), and matching on (scenario, repeat) cancels that. Concordant pairs
carry no information about the difference and are ignored. With 24 scenarios x 5 repeats there
are up to 120 pairs per model; the smallest splits that pass are 6-0 (p = 0.031), 8-1 (0.039),
10-2 (0.039) and 12-3 (0.035), while 5-0 (0.063), 7-1 (0.070) and 9-2 (0.065) do not. The
report also lists every other variant against control; those rows are context, only the
candidate can ship (`--candidate` and `--control` rename the pair).

## Cost per full run

cells = variants x scenarios x models x repeats = 5 x 24 x 2 x 5 = **1200**. Measured on the
2026-09-10 run: mean 22.4 s per valid opus cell (max 44 s) and 16.4 s per sonnet cell (max
54 s); the isolated opus cell above took 22.9 s, so the copy adds nothing measurable. 600 x
22.4 s + 600 x 16.4 s is about 6.5 h of cell time, **about 1.6 h at `--parallel 4`**. Probes
take about 14 s per model. Each cell is one short `claude -p` conversation on the
subscription; there is no per-token bill, but the cells draw on the subscription's usage
limits.

A usage-limit or auth error in any cell stops the whole run (exit 2, nothing recorded for that
cell) rather than writing a hundred failures; rerun the same command to resume. Only stderr is
scanned for that on a successful cell; the model's answer is scanned on failed cells alone,
because the fixture is an auth gateway and answers routinely say "unauthorized" or "401".
Streams carry `rate_limit_event` lines with `status: allowed_warning` near the limit; those are
informational and not treated as a stop.

## The `[memware-eval]` marker

From docs/eval.md: `[memware-eval]` (`memware.eval.MARKER`) in an evaluation prompt lets
`memware-eval --corpus` rebuild a clean store that skips any transcript carrying it, and
`memware prune --containing '[memware-eval]'` removes stragglers from a live store. The first
run sent it through `--append-system-prompt`. Grid runs and probes no longer do: the line sits
in the model's system prompt and tells it that it is being evaluated, while
`--no-session-persistence` means no transcript exists for it to mark and
`MEMWARE_NO_CAPTURE=1` covers a hook that ever runs. `--marker` sends it again (when
`claude --help` lists the flag); use it only for runs whose transcripts are persisted.

## Result rows

One JSON line per cell:

```
variant, scenario, class, expect, model, repeat,
tools_called (ordered), tool_inputs, first_tool,
recall_called, recall_logged, recall_mismatch,
recall_count_transcript, recall_count_stub, count_mismatch, stub_calls,
recall_queries (first recall call), n_phrasings, turns, permission_denials, result_subtype,
elapsed_s, final_text (300 chars), rc, error, invalid_reason, out_of_copy, valid,
copy_dir, dirs_removed, memory_paths, isolation, marker, ts
```

- `tool_inputs`: one object per `tool_use` block, `{name}` plus whichever of `path`,
  `pattern`, `file_path`, `glob` the input had.
- `invalid_reason`: `out_of_copy` (takes precedence), `timeout`, the result subtype of a failed
  run (for example `error_max_turns`), or `rc N`; `error` holds the detail and `out_of_copy`
  the offending inputs. `valid` is true only when `invalid_reason` is null.
- `memory_paths`: non-empty memory fields from the cell's init event; `{}` everywhere, or the
  auto-memory switch has stopped working.

`score.py` excludes invalid cells and counts them by reason (docs/eval.md, item 8: mark invalid
answers, do not score them). Rows written by the first harness lack the new fields; `score.py`
derives their reason from `result_subtype`, `error` and `rc`.
