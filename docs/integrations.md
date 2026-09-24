# Integrations

## Claude Code

The plugin's hooks call `memware` as a bare command, so install the CLI on your PATH
**as a tool** first (a plain `pip install` into a project/conda env usually leaves it off
the hook shell's PATH, and the hooks then silently do nothing):

```bash
uv tool install "memware[mcp]"    # or: pipx install "memware[mcp]"
memware --version                  # must resolve
```

Then add the plugin (the repository is its own marketplace):

```bash
claude plugin marketplace add ericwalisko/memware
claude plugin install memware@memware
```

Hooks (`hooks/hooks.json`):

| event | command | effect |
|---|---|---|
| `SessionStart` | `memware sync` (catch-up) + `memware backup --if-stale 20`, then `memware derive --apply --auto --if-stale 24` (a no-op until `memware config derive.auto true`), all backgrounded | indexes any session whose `SessionEnd` never ran, then a throttled backup — see note |
| `SessionStart` | `memware notice --from-hook`, in the foreground | until `memware setup` has asked about `derive` (setup last ran before 0.4.0 or never, and `derive.auto` is unset), shows one line under the session header saying so; reads only the config, and prints nothing once either is true, after a compaction, or when the config will not parse |
| `SessionStart` | `memware digest --from-hook`, in the foreground (5 s timeout) | injects a block of at most 1,200 characters: a line pointing at `recall`, this project's 5 most recent sessions (date and first prompt), and the beliefs whose subject names the project, each with the date it was recorded, less the stale ones ([below](#what-injection-leaves-out)); nothing for a project memware has no session for — see [the digest](#the-session-start-digest) |
| `SessionEnd`, `PreCompact` | `memware sync --harness claude-code --from-hook` | indexes the session's new turns from `transcript_path` |
| `UserPromptSubmit` (optional) | `memware context --from-hook` | injects the few beliefs whose subject the prompt names, each with the date it was recorded, less the stale ones ([below](#what-injection-leaves-out)), as `additionalContext`; with the [optional relevance filter](../README.md#optional-a-relevance-filter-for-prompt-time-injection) switched on, less what it judges irrelevant too |

`SessionEnd` runs when Claude Code exits cleanly, but some environments **force-kill** it (a worktree/pane manager may `SIGKILL` the process group on close), and a `SIGKILL` cannot run any hook. The `SessionStart` hook covers that: it runs a bare `memware sync` — which catches up the configured `backup.transcript_src` (default `~/.claude/projects`) — plus a throttled backup, **backgrounded** so it never delays startup. So the previous session is indexed at the next start even if its `SessionEnd` was skipped; the raw transcript is durable on disk regardless.

That catch-up and the backup run without the environment of the session they pick up, so a session started with `MEMWARE_NO_CAPTURE=1` is kept out another way. Every `--from-hook` entry that runs under the variable (`notice`, `digest`, `context`, `sync`) adds the payload's `transcript_path` to `<home>/no-capture.txt`, and every sync and backup skips what is listed. In payloads captured from Claude Code 2.1.268, `SessionStart`, `UserPromptSubmit` and `SessionEnd` all carry `transcript_path`, even with `--no-session-persistence` (the file is then never written); `PreCompact` did not fire in that probe. The start entries record the path before the first prompt. Recording prints nothing and never changes a hook's output. See [keeping-memory-clean.md](keeping-memory-clean.md#what-the-switch-cannot-do) for what it cannot catch.

A backgrounded hook's output reaches nobody, so the notice is a separate foreground entry. Claude Code shows its `systemMessage` to you, not to the model: someone who only uses the plugin never runs `memware stats` and would otherwise never learn that `derive` exists. It takes a few tens of milliseconds, and it exits 0 with no output if `memware` is missing from the hook's `PATH` or is too old to know `notice`.

The prompt-time hook injects **beliefs only**, capped by `-k`. Transcript
search is on demand through the MCP server. Add it at **user** scope so every
project sees it — the default (`local`) scopes the server to the one directory
you run the command in, and sessions in other projects won't find the tools:

```bash
claude mcp add -s user memware -- memware-mcp
claude mcp list        # confirm it's registered; tools load at the next session start
```

On a new machine, index existing transcripts once with `memware backfill` (defaults to
`~/.claude/projects`, idempotent); the plugin only captures sessions from then on.

Tools: `recall` (takes a list of phrasings — have the agent pass 3–5, including synonyms and the literal value it expects), `read_session`, `beliefs`, `remember`, `pending_reviews`.

Subagents: the plugin does not inject into subagents. They can call the MCP
tools. Their transcripts are synced with the parent session's.

### The session-start digest

The prompt hook injects nothing until the ledger has beliefs, so for most sessions the digest is
the first memware content the model sees. That matters because the model only recalls when it
chooses to. Run `memware digest` in a project directory to see the block the hook injects. Like
the notice, the hook exits 0 with no output if `memware` is missing from the hook's `PATH` or is
too old to know `digest`.

The digest is scoped by transcript path, with no search. Claude Code keeps a project's
transcripts in `~/.claude/projects/<the directory, every non-alphanumeric character a dash>/`
(under `$CLAUDE_CONFIG_DIR` when that is set), so only sessions from that directory count.
Inside a git repository the project is the whole repository: the primary checkout and every
live linked worktree, read from git's own files. The block lists the most recent sessions
(`-k`, default 5) and the current beliefs whose subject shares a whole word with the
directory name, the repository name, or the package name in `pyproject.toml`, `package.json` or
`Cargo.toml`, less what [injection leaves out](#what-injection-leaves-out), up to `--max-chars`
(default 1,200). The session that is starting is left out.
Nothing the digest reads counts as a recall.

Two limits:

- **Claude Code transcripts only.** Other harnesses have no per-project transcript layout, so
  sessions indexed from Hermes or with `--harness generic` never appear in the digest. They
  remain searchable through `recall`.
- **Removed worktrees drop out.** Git stops listing a worktree once it is removed, so sessions
  held only in a torn-down worktree leave the digest. `recall` still finds them.

### What injection leaves out

Both blocks are unsolicited, so they carry only what is likely still true. A belief closes when a
later value supersedes it, and a snapshot is never superseded: a row count, the version a branch
was at, a PR's status each stay current in the ledger long after they are wrong. The prompt hook
and the digest leave out:

| reason | what (derived beliefs only, and only unambiguous ones) | example |
|---|---|---|
| `measurement` | a relation that is exactly progress, coverage or null rate; a count, total or "number of" over rows, records, tests, files, lines, commits, duplicates, accounts, users or downloads; a magnitude or a comma-grouped number of 1,000 or more beside one of those; an "N of M" over one, in the relation or as the subject's noun, or over a completion word ("backfilled"); not when the relation says it must hold ("must pass", "at least") | `memware test suite test count: 91 tests`, `appointment rows eligible and exported: 2,454 of 10,346` |
| `moving_version` | a version string the subject or relation calls current, latest, built, installed, deployed, released or on main | `memware main branch current version: 0.4.0` |
| `status` | a relation that is exactly status, state or progress, whose value is a status word (open, merged, review, blocked, failing, connected, …) or whose subject names an instance (`#12`, `t_cd03d14d`, or ending in run, scan, build, job, PR, issue or card); a relation ending in status, whose value is a status word or where an instance id (`#31`, `t_…`) is named; a relation that names a finding or a defect (known issue, open issue, must-fix issue, should-fix issue, blocker), unless the value points at where it is tracked or states a by-design limitation or a workaround | `card t_cd03d14d status: review`, `memware PR #31 ci status: green`, `memware known issue: …` |
| `contradicted` | inside a project: a belief about a package's own version that differs from the version that package declares | `built memware wheel version: 0.5.0` in a 0.6.1 checkout |
| `older_version` | the same: a belief naming an older version beside the package's name | `memware 0.4.0 known issue: …` |

**Precision over recall.** Hiding a durable fact silently removes something you relied on; a
stale belief that slips through is how memware behaved before, and `memware beliefs retract ID`
removes it. So these rules catch only what is unambiguous, and a qualifier, checked before every
rule, means durable: slo, sla, target, threshold, budget, commitment, fail under, min, max, limit,
default, initial, final, required, desired, every, schedule, retention, pin, check, and their like,
as any word of the relation or a whole word of the subject (`api p99 latency slo: 200ms`, `ci
status check: required`, `required ci coverage: 90%`, `nightly backup cron runs every: 6 hours`,
`export job scheduled row count: 4,200 rows`). An identifier in the subject is a name:
`scheduled_export` is an export, not a schedule. One that names a setting still counts: its last
part is the qualifier (`export-schedule`, `ruff-pin`), it holds a bound (`min_coverage`), or it
joins a qualifier to what would be measured (`max_rows`, `page_size`). A compound "… state" is a
design term (`sync indicator error state: red`), and a finding that points at its tracker or
states a by-design limitation stays. Anything else in doubt is durable too: `main branch python
version: 3.12`, `feature flag dark_mode state: enabled`, `rollout percentage: 10%`. The fuzzy judgment belongs to derive's prompt, which sees the excerpt;
this sees only a triple. `tests/data/volatility_cases.jsonl` is the labeled corpus the rules are
held to: no durable case may be left out, and the volatile cases they miss (`api p95 latency:
340ms`, `memware sync at 50k turns latency: 3.7 s`, …) are listed there, marked.

To see why one belief is or is not injected, `memware beliefs --explain ID` prints its class or
durable, the test that decided it (with the qualifier and the field it came from, for a veto),
each exemption and the manifest check, and `injected : yes` or `no`. `memware beliefs --explain
--subject S --relation R --value V` judges a triple that is not in the ledger, with no store.

The manifest is `pyproject.toml` (`project.version`, a hatch `[tool.hatch.version] path` holding
`__version__`, or poetry), `package.json` or `Cargo.toml`, read from the root of the project the
hook's `cwd` is in; a monorepo's nested packages are not read. A belief is checked only against
the version declared by the package its subject names, never another package's. A version a build tool
computes (a `dynamic` version with no file to read, setuptools-scm) and a `0.0.0` placeholder
are never checked against. The classification is regex and word lists: no model call and no
network, because the prompt hook runs it on every prompt.

A person stating a fact is a decision to keep it. A belief whose reliability is above derive's
0.5, or whose source is not a `memware:session/` pointer (`remember`, `memware assert`), is
never left out. Neither is a derived belief a person has confirmed: asserting the same value
again, through `memware assert` or `remember`, or approving it in `memware review`, records a
confirmation beside it (the belief row keeps its session source), and from then on it is
injected. That is how to keep a fact the gate left out.

`memware config inject.volatile_days N` injects a `measurement`, `moving_version` or `status`
belief while it is younger than N days. The default is 0, never: the reported version belief
was one day old and already wrong. A `contradicted` or `older_version` belief is left out
whatever its age.

Nothing is hidden. `memware beliefs`, `recall` and the MCP tools still return these beliefs,
each with its date and `volatile` naming its class. `memware beliefs --stale` lists what is left
out and why, `memware beliefs retract --stale` (a dry run until `--apply`) retracts it, and
`memware stats` counts it by reason. The session-start notice tells an upgrading user once how
many beliefs are no longer injected; the store records that it did.

The Hermes provider's `prefetch` applies the class rule and the window through each hit's
`volatile` mark. It has no project directory, so the manifest rules do not apply there.

The [relevance filter](../README.md#optional-a-relevance-filter-for-prompt-time-injection) is
off by default. When `relevance.mode` is `filter`, it also leaves out candidates that TypeSafe's
classifier judges irrelevant to the prompt. It works on what the gate admitted, widened to
`relevance.pool` candidates, and never adds a belief the gate left out. Both the prompt hook and
`prefetch` read the switch from memware's own config. In `shadow` mode it logs its judgments and
leaves injection unchanged. A task notification, a hook fired inside a subagent (its payload
carries `agent_id`) and a session memware keeps out of the store are never sent. With the filter
off, a task notification still gets beliefs injected, as it did before the filter existed.

## Hermes Agent

`integrations/hermes/memware/` is a memory-provider plugin implementing
[Hermes Agent](https://github.com/NousResearch/hermes-agent)'s `MemoryProvider`
ABC: prompt-time belief `prefetch`, non-blocking `sync_turn` capture, session
flush, built-in-memory mirroring, and four tools for iterative recall. Install by
copying it to `$HERMES_HOME/plugins/memware/` and running `hermes memory setup`.
Both plugins share one store by default, so Claude Code and Hermes remember the
same things.

`integrations/hermes/upstream/` stages the same provider packaged as
hermes-agent's own `plugins/memory/<name>/` tree, for contributing it in-tree so
`hermes memory setup` lists memware on a clean install with no manual copy. It
is prepared, not submitted — see that directory's README for what is still open
and `docs/upstream-hermes-pr.md` for the draft PR text.

## Any other harness

Export sessions as message JSONL (`role`, `content`, `timestamp`, optional
`session`) and run `memware sync <dir> --harness generic`. Add a parser under
`memware/ingest/` for a native format — it is one generator function.

## Review channel

Contested supersessions are published through a `ReviewBackend`:

- `memware review sync` — JSONL outbox/inbox under `~/.memware/`
- `memware review sync --url https://your.host/memware --token …` — HTTP:
  `POST /reviews`, `GET /decisions`

Implement either side in whatever tool you use to make decisions.
