# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **`derive` no longer files a measurement as a durable belief, and keeps a short setting**
  ([#38](https://github.com/ericwalisko/memware/issues/38)). The prompt rejected events,
  predictions and intentions, but nothing rejected a dated quantity, so `the table | row count =
  4.2 million rows` became a permanent fact while `The retry limit is now set to 5` was dropped
  as `value too short`. The prompt now rejects measurements (counts, row totals, percentages,
  rates, sizes, durations, "N of M" figures) and transient state (the version a branch, build or
  install is currently at; the status of an issue, PR, run or check; what a PR contains) on the
  same footing as events, with one test for both: would the value need re-checking to know it is
  still true? Its vague-subject rule now has a concrete test: a determiner plus a generic noun
  with no name ("the table", "the repo", "the worker") names nothing. The deterministic gate
  backs both with no model call: it rejects a generic-noun subject, and a triple that
  `memware.volatile.classify` names a measurement, a moving version or a status, so a weaker
  model still writes less rather than wrong. A one-character value is admitted when it is a
  number and the relation names a setting (`retry limit = 5`).
- **The prompt hook and the session-start digest stop injecting beliefs that were true when
  recorded and are wrong now.** A derived belief closes only when a later derive supersedes the
  same key, which rarely happens, so `memware main branch current version: 0.4.0`, `built memware
  wheel version: 0.5.0` and `memware test suite test count: 91 tests` reached every session under
  "Known facts (currently valid…)" long after the repository moved on. None of them is an orphan,
  so `retract --orphaned` and `prune` never touched them. Both unsolicited readers now leave out:
  - a derived belief `memware.volatile` classifies as a measurement, a moving version or a
    status. It is left out by default; `memware config inject.volatile_days N` injects one while
    it is younger than N days. The default is 0 (never) because the reported version belief was
    one day old and already wrong, so no multi-day window would have kept it out;
  - inside a project whose manifest declares a version (`pyproject.toml`, including a hatch
    `[tool.hatch.version] path`; `package.json`; `Cargo.toml`), a belief about the project's own
    version that differs from it (**contradicted**), and a belief naming an older version of
    the project beside its name, such as `memware 0.4.0 known issue` (**older version**).
  A person stating a fact is a decision to keep it: a belief with reliability above derive's
  0.5, or a source that is not a `memware:session/` pointer (`remember`, `memware assert`), is
  exempt from all of it. Nothing is deleted or hidden: a left-out belief stays in `memware
  beliefs`, `recall` and the MCP tools, which now mark it with `volatile` (its class). The
  classification is regex and word lists, no model and no network, because the prompt hook runs
  it on every prompt.

### Added
- **`memware beliefs --stale`** lists the current beliefs injection leaves out, each with its
  reason (`measurement`, `moving_version`, `status`, `contradicted`, `older_version`) and why
  (`pyproject.toml says 0.6.1`). `--cwd DIR` names the project whose manifest is checked.
- **`memware beliefs retract` takes `--stale` or belief ids**, as a dry run unless `--apply`, as
  `--orphaned` does. Rows are kept and each retraction records its reason. Neither reopens what
  a retracted belief had superseded, because that value is older still; a predecessor is
  relinked to the next belief that survives, as `--orphaned` does.
- **`memware stats` counts the beliefs injection leaves out, by reason** (`injection.left_out`
  under `--json`), with the window and the manifest version it read.
- **An upgrading user is told once.** The session-start notice says how many beliefs are no
  longer injected and names `memware beliefs --stale`. It reads the store once, records that in
  `<home>/notices.json`, and never creates a store.

### Changed
- **The injected blocks say what they are.** The prompt hook's header is now `Known facts from
  your memory ledger, each with the date it was recorded:` and the digest's `Beliefs about this
  project from your memory ledger, each with the date it was recorded:`; each line ends
  `(recorded YYYY-MM-DD)` instead of `(since …)`. Neither claims a fact is currently valid.

## [0.6.1] - 2026-09-16

### Changed
- **`memware prune --turns-containing` matches text anywhere in a turn, and the prefix match it
  used to be is `--turns-starting-with`** ([#32](https://github.com/ericwalisko/memware/issues/32)).
  `--turns-containing X` removed only turns that began with X, so a value pasted
  mid-conversation (a token, a password) could not be selected, and the dry run answered
  `turns to remove : 0` for a store that still held it. It now selects every turn holding X. The
  new `--turns-starting-with X` keeps the prefix match, which is still the right tool for a
  recurring harness preamble. Both match literally and case-sensitively, as `--containing`
  does: `_` and `%` are no longer wildcards, and `rollout` no longer matches `Rollout`. A turn
  selector takes no other selector, and combining one with `--glob`, `--containing` or the other
  turn selector exits 2 (`--glob` and `--containing` used to be ignored silently). The library
  follows: `prune(turns_containing=…)` is a substring match, `prune(turns_starting_with=…)` is
  new, and `prune_turns` takes exactly one of `containing` and `starting_with`.
  **Migration:** a script or cron that runs `memware prune --turns-containing PREFIX` to strip a
  boilerplate prefix should run `--turns-starting-with PREFIX` instead, because a substring
  match also removes every turn that quotes the prefix. A library call
  `prune_turns(store, containing=PREFIX)` becomes `prune_turns(store, starting_with=PREFIX)`.

### Fixed
- **`memware prune --containing` reads the whole transcript**
  ([#33](https://github.com/ericwalisko/memware/issues/33)). It decided by the first 200 KB of
  each file, so a value past that point, which is most of a long session, was never found and
  the dry run reported `sources to un-index : 0`. It now reads every file to the end, a megabyte
  at a time, with an overlap so a value that spans two reads still matches. Sync's skip markers
  and the backup mirror still check only a transcript's head, as documented.
- **A prune that matches nothing says what it searched.** A bare `0` read the same as a clean
  store. When a selector matches nothing, `prune` prints to stderr what it searched and how
  (`no turn starts with 'X' (1,204 searched, matched literally and case-sensitively); 2 turns
  contain it past the start: try --turns-containing`), and names what another selector would
  reach: turns that hold the text past their start or in another case, and indexed turns that
  still hold a value no transcript file on disk does. `--containing` also names the indexed
  sources it could not read because the transcript file is gone, and `--turns-starting-with`
  the turns it leaves holding the text past their start. Standard output, `--json` and
  `--plain` are unchanged.
- **`memware stats` no longer conflates a retractable orphan with a stale citation.** The
  `beliefs from no indexed turn` line merged two different counts: beliefs whose cited session
  has no turn left at all (`memware beliefs retract --orphaned` acts on exactly these), and
  beliefs whose cited turn id no longer exists even though the session is still indexed — a
  citation left dangling by re-indexing, not evidence that is actually gone. It read as "cleanup
  needed" even when the retractable count was zero. The line is gone; `stats` now prints
  `beliefs citing an unindexed session` and `beliefs with a stale turn citation` separately, the
  retract call-to-action fires only for the first, and `--json` exposes both under
  `utilization.beliefs_orphaned` and `utilization.beliefs_stale_turn`.

## [0.6.0] - 2026-09-16

### Added
- **Beliefs whose evidence was un-indexed are retracted.** A derived belief cites
  `memware:session/<id>/turn/<n>`. When that session left the index, the belief stayed
  committed and went on reaching prompts as a known fact. A new status, `retracted`, closes a
  belief at its own start (`valid_to` = `valid_from`, as a rejected candidate is closed). The
  belief leaves `recall`, the prompt hook, the digest, `memware beliefs` and the
  `beliefs_current` count. It stays in the history of its key with when and why it was
  retracted, kept in a new `retraction` table beside `belief`, which an existing store gains on
  open with no migration. No belief row is deleted. If the retracted belief had superseded a
  committed value, that value becomes current again. If a later belief had already superseded
  the retracted one, the older value closes where that later one starts. A belief whose source
  is not a session pointer (anything a person passed to `remember` or `assert`) is never
  retracted. Beliefs whose source names the session in free text are listed as kept. A value
  that arrives late, dated before the current one, skips retracted beliefs when it takes its
  place in the timeline, as it skips rejected ones, so it never reinforces a retracted row.
- **`memware prune` cascades into the ledger.** It retracts the beliefs derived from every
  session it leaves with no turn, in the same transaction as the delete. That covers whole
  sources and `--turns-containing` alike, and the library's `prune_sources` and `prune_turns`
  do it too. The new `memware.ingest.prune` takes `apply=False` for a plan.
- **`memware beliefs retract --orphaned`**: the one-shot for beliefs whose cited session is
  already gone, from an earlier prune or from a sync that skipped a listed or marked
  transcript. A sync never changes a belief. The command has the same dry-run and `--apply`
  shape as `prune`.
- **`memware stats` counts beliefs citing an unindexed session**
  (`utilization.beliefs_orphaned`), with a verdict line naming the one-shot when there are any.
- **`capture.exclude`: path globs that no sync indexes and no backup mirrors.** `MEMWARE_NO_CAPTURE`
  reaches only the processes a run starts, and a marker needs its text inside the transcript, so a
  generator whose author forgot both was captured. A pattern lives in the machine's config and
  names a run by the directory it ran in. It is matched against the whole resolved transcript
  path, as `memware prune --glob` matches (`*` crosses `/`, a leading `~` is expanded), so
  `*/-Users-me-gen-runs/*` covers a Claude Code project directory's sessions and their subagents.
  Every sync (`sync_file`, `sync_tree`, the hooks, the catch-up, `backfill`, `setup`, the Hermes
  provider) skips a matching transcript and un-indexes it if it was indexed before, exactly as for
  a skip marker. `memware backup` never mirrors it, counts it under `transcripts_skipped_glob`
  beside the no-capture and marker counters, lists copies an earlier run made under
  `transcripts_left_in_backup`, and never deletes from the destination. The three layers sit side
  by side, and a transcript is counted under the first that names it.
- **`memware exclude`** lists the patterns with the transcripts on disk and the indexed sources
  each one matches, and the share of the transcripts on disk they hide together. `--add GLOB` and
  `--remove GLOB` print what would change and write nothing without `--apply`; the dry run opens
  the store read-only and never creates one. `--apply` writes `capture.exclude` and un-indexes
  every indexed source the patterns match, including sources whose transcript has left the disk,
  which no sync would visit again. Like a sync, it retracts no belief; it reports how many beliefs
  now cite an unindexed session, for `memware beliefs retract --orphaned`. Removing a pattern
  re-indexes nothing by itself; the next sync does.
- `memware stats` shows the patterns and how many transcripts on disk they hide (`capture` in
  `--json`; the tree is walked only when a pattern is set). `stats`, `exclude` and `backup` warn
  when the patterns hide half or more of the transcripts on disk.
- `docs/keeping-memory-clean.md` gains Layer 3 and a table of how the no-capture list, path
  exclusions and skip markers relate, with the guidance that a generator should run from a
  working directory of its own so a pattern can name it without excluding interactive sessions.

### Changed
- **`memware prune` is a dry run unless `--apply` is given.** It prints the sources and turns
  it would remove, the beliefs it would retract, the predecessors it would reopen or relink, and
  the stated beliefs it keeps, and writes nothing. Scripts that ran `memware prune` to delete
  must add `--apply`. `--json` keeps `sources_pruned` and `turns_removed` and adds `applied`,
  `sessions_emptied`, `retract`, `reopen`, `relink` and `keep`.
- `memware prune` needs one of `--glob`, `--containing` or `--turns-containing` and exits 2
  without one. With none, it used to un-index every source.
- **`memware derive` reads interactive sessions only by default.** Claude Code writes an
  `entrypoint` on every transcript record (`cli` for an interactive session, `sdk-cli` for
  `claude -p`), and a new `turn.entrypoint` column keeps it; the Claude Code parser sets it and
  parsers that cannot know leave it NULL. The new setting `derive.sources` defaults to
  `interactive`, which skips turns from `claude -p`, the Agent SDKs (`sdk-ts`, `sdk-py`), the
  GitHub Action and `mcp serve`. Those turns stay indexed and recallable; they no longer become
  beliefs. On one machine 1,561 of 1,719 transcripts were `claude -p` sessions, eval scaffolding
  among them, so a store that derived from everything filed facts from prompts. Headless runs
  that hold real decisions opt back in with `memware config derive.sources all`. A turn with no
  entrypoint, and one with an entrypoint the list does not name (an IDE extension, the desktop
  app), is read as interactive, so the default never drops what it cannot label. The watermark
  still moves past skipped turns: switching the setting changes what the next run reads, and
  `--since` re-reads older turns. A value other than `interactive` or `all` makes derive exit 2
  before any provider is built.
- `memware derive --plan`, and a run, print the setting and how many new turns each setting would
  read (`turns : N under interactive, M under all`), so the cost of the choice is visible before a
  run. The plan's `--quiet` summary gains those two lines.
- **Schema version 3.** An existing store gains `turn.entrypoint` on first open, with no row
  removed, and its indexed Claude Code turns take the entrypoint their transcript's first
  conversation record carries, if the transcript is still on disk. Turns whose transcript is gone
  stay NULL. A configured backup destination gets a safety snapshot first, as for every
  migration. `memware read --json` rows carry the new field.
- **`memware stats` shows where the store came from**: sessions, turns and current beliefs by
  entrypoint (a belief counts under the turn its source pointer names), beliefs from no indexed
  turn, and the five project directories holding the most sessions with their share, so a
  generator that wrote much of the store is visible without an audit. `--json` adds
  `provenance` and `derive.sources`; `derive.turns_pending` counts what the setting reads.
- `memware beliefs retract` with no relation is now the retract command. A key whose subject is
  `retract` still reads as history with `memware beliefs retract RELATION`.

## [0.5.0] - 2026-09-15

### Changed
- **MCP tool descriptions say when to call them, not only how.** Recall fires only when the
  model elects to call it, and it competes with grep, so its description now opens with an
  explicit "Call this when" list (past decisions and their rationale, rejected alternatives,
  why something is the way it is, earlier sessions, cross-repo, anything not in the working
  tree, and before answering "I don't know") and a "Not for" line (code in the tree: grep
  it), with the phrasing how-to tightened behind it, all under 130 words. `beliefs` and
  `read_session` each gained a trigger line. A test holds the budget and the triggers. The
  Hermes provider's tool schema is unchanged.
- The README, `docs/scheduling.md` and `memware derive --help` no longer describe the dry run
  as side-effect free. A dry run sends every excerpt to the provider and skips only the write.
  `--plan` is the view that sends nothing. `--apply` and `--plan` are mutually exclusive.
- `memware derive --chunk` rejects values below 1 instead of silently treating 0 as the default.
- `memware stats` human output is labeled `field : value`, one per line, like the record
  listers (it was one line of JSON). `stats --json` is a superset: the existing keys are
  unchanged, and the new sections are nested under `derive` and `utilization`.
- Belief activation no longer grows from hook injection: `memware context` (the
  `UserPromptSubmit` hook) no longer records a use for the beliefs it injects, so `use_count`
  and `last_used` mean an agent or a person retrieved the belief.

### Added
- **`memware digest`, injected at session start.** The prompt hook injects beliefs only and
  nothing until the ledger fills, so most sessions never saw memware content, and a model that
  has not seen it rarely calls recall. A foreground `SessionStart` entry beside the notice
  (`memware digest --from-hook`, 5 s timeout) now injects one block of at most 1,200
  characters. It opens with "memware has N sessions and M beliefs for this project; call recall
  for past decisions, earlier sessions, anything not in the working tree." It then lists the
  project's 5 most recent sessions, each as its date and first prompt clipped to 120
  characters, and the currently valid beliefs whose subject shares a whole word with the
  directory, repository or package name (`pyproject.toml`, `package.json`). The project is
  scoped by transcript path, the way Claude Code names its per-project directories, and
  answered from the existing `(source, seq)` index with no search. Inside a git repository the
  primary checkout and every live worktree count as one project. The starting session is left
  out, and nothing the digest reads counts as a use. It prints nothing for a project with no
  indexed session or when there is no store, and never creates one. `-k`, `--max-chars` and
  `--cwd` change the defaults. Only Claude Code transcripts are covered, and sessions from a
  removed worktree drop out of the digest but not out of recall (`docs/integrations.md`). Like
  the notice, the hook exits 0 with no output when `memware` is missing or too old to know
  `digest`.
- **`memware derive --plan`** — the no-network view of a run. It gathers the excerpts exactly as
  a run would (same watermark, session cap and dedupe) and prints each one with its session and
  source pointer. It then prints the session, excerpt, character and model-call counts and the
  destination (provider and model, or the `OPENAI_BASE_URL` endpoint), and stops before any
  provider exists. No model call, no writes, no run lock. It works with no `claude` on PATH and
  no `OPENAI_*` and says what a real run would still need. Honours `--since`, `--max-sessions`,
  `--provider`, `--model` and `--chunk`; `--quiet` keeps only the summary.
- **`memware stats` says whether memory is doing anything.** Two new sections beside the six
  counts. *derive*: whether `derive.auto` is on, runs, the last run and its age, the watermark
  against the latest turn id, and how many turns are not yet derived — "never run" when there
  is no state file. *utilization*, from the `use_count`/`last_used` columns recall already
  writes: beliefs and turns recalled in the last 7 and 30 days, how many turns were ever
  recalled (and what share of the store that is), and when anything was last recalled.
- A plain-language `verdict` line in the human output when the store is inert: turns but an
  empty ledger because derive has never run; `derive.auto` off with the last run over 30 days
  old and turns waiting; or nothing recalled in 30 days. A derive that ran and found nothing
  no longer looks the same as one that never ran. `--json` carries the same facts as fields
  and prints no verdict.
- **`memware setup` asks about `derive`.** After the backup steps, setup says what derive
  does and where the excerpts go: Haiku through the Claude Code CLI on your own
  subscription, or the OpenAI-compatible endpoint you configured. It points at
  `memware derive --plan` for a preview that makes no network call, then asks whether to
  switch on the automatic run. Setup writes either answer, so a decline sticks. `--yes`
  and a closed stdin leave `derive.auto` unchanged, so setup never switches it on
  without an answer.
- **The Claude Code plugin says `derive` exists, at session start.** Someone who only uses
  the plugin never types a memware command, so the hint `stats` prints never reached them.
  A new foreground `SessionStart` entry runs `memware notice --from-hook`, and Claude Code
  shows the same hint as a line under the session header. It shows at every session start
  until setup has asked (setup runs on 0.4.0 or later) or `derive.auto` is set either way.
  It reads only the config file, never the store. It prints nothing after a compaction or
  when the config will not parse. It exits 0 with no output when `memware` is missing or
  too old to know `notice`. `memware notice` prints the same lines in a terminal.

### Fixed
- **`MEMWARE_NO_CAPTURE` sessions were indexed and copied to the backup folder.** The variable
  was checked only by `memware sync --from-hook`, in the one process that carried it. The
  plugin's `SessionStart` catch-up (`memware sync`, since 0.2.6) runs in the next session without
  it and indexed every such transcript still on disk; `memware backfill` and `memware setup` did
  the same. The transcript mirror in `memware backup` (since 0.2.0) copied them, and it also
  copied transcripts carrying a skip marker, into `<dest>/transcripts`, which is often a synced
  folder. The Hermes provider never checked the variable. Now every `--from-hook` command that
  runs under the variable (`sync`, `context`, `notice`, `digest`) adds the payload's
  `transcript_path` to `<home>/no-capture.txt`. The start and prompt entries record it, so a
  force-killed session is covered, and recording never changes a hook's output. Every sync skips
  a listed transcript and the subagent transcripts Claude Code keeps beside it in
  `<session>/subagents/`, and un-indexes any of them indexed before. The mirror skips the same
  files and marker-tagged transcripts. `memware backup` reports `transcripts_skipped_no_capture`
  and `transcripts_skipped_marker`, plus `transcripts_left_in_backup` for copies an earlier run
  made, which it never deletes. The Hermes provider captures nothing under the variable. Limits: a
  session that ran no memware hook and carries no marker still cannot be recognised, and sessions
  indexed or mirrored before this release cannot be identified afterwards. Un-index known paths
  with `memware prune --glob`/`--containing` and delete their copies from the backup folder by
  hand (docs/keeping-memory-clean.md). `claude -p --no-session-persistence` writes no transcript.
- `memware setup` printed the mirror's result object instead of the number of transcripts it
  mirrored in its first-backup line.
- `derive --if-stale` read the last run as an hour older than it was whenever the local zone
  was on daylight time; the age is now UTC arithmetic.
- Upgrading to 0.4.0 never mentioned `derive`. Derive is off by default, which is right,
  since it sends transcript excerpts to a model. But nothing said it existed: the setup
  hint only checked that setup had run at some point, never on which version. `init`,
  `backfill` and `stats` now print a one-line hint when a version added a feature that
  sends data to a model and setup has not asked about it yet. For `derive` that means
  setup last ran before 0.4.0 (or never) and `derive.auto` is not set. The hint stops once
  setup runs on 0.4.0 or later, or once `derive.auto` is set to either value. It stays
  silent from hooks and with `--json`.
- `memware config KEY VALUE` and `memware setup` now write only the keys you set. Both
  used to save the whole merged config, which recorded every default as if you had chosen
  it. A default changed in a later release then never reached you, and "never asked about
  derive" looked the same as "declined". Configs saved that way on 0.4.0 already hold
  `derive.auto: false`, so those installs will not see the new hint.

## [0.4.0] - 2026-09-09

### Added
- **`memware derive`** — fill the belief ledger from the transcripts. Reads every turn
  indexed since its last run, has a model propose `(subject, relation, value)` triples for
  the sentences that look like durable facts, and files only those that pass a
  deterministic groundedness check (every word of the value must occur in the excerpt).
  Incremental (watermark beside the store), dry run by default, `--apply` to write.
  Providers: `claude-code` (default — the Claude Code CLI on your subscription, Haiku,
  nothing to configure) and `openai` (any OpenAI-compatible endpoint via `OPENAI_*`).
  Exit codes 2 (not configured) and 4 (provider unavailable, nothing written) are the
  scheduling contract.
- **`memware config derive.auto true`** lets the Claude Code plugin's session-start hook
  run `derive` in the background, at most once a day — no cron, no always-on machine.
  `docs/scheduling.md` covers LaunchAgents (catch up after sleep), systemd timers
  (`Persistent=true`), and plain cron for those who want a clock instead.

### Fixed
- **`memware backup` no longer aborts on one unwritable transcript.** The mirror opened
  each target with `shutil.copy2`, and a synced destination (Dropbox observed) evicts
  already-uploaded files to dataless placeholders — opening one of those for write makes
  the sync engine materialise it first, which failed with `OSError: [Errno 11] Resource
  deadlock avoided` roughly half the time. That took down the whole mirror (and the
  cron/hook exit code) even though the snapshot before it had already succeeded. The mirror
  now copies to a temp file beside the target and `os.replace`s it in, so a target is never
  opened for writing directly, and a file that still can't be written is skipped and
  reported (`transcripts_skipped` in `--json`, one stderr line each) rather than raised —
  best-effort, retried on every run.

## [0.3.0] - 2026-09-04

### Added
- **`--plain` output** for `recall`, `beliefs`, and `read`: tab-separated, id-first, one
  record per line (tabs/newlines in values collapse to spaces), for piping to fzf/awk/cut.
- **Shell completions**: `memware completions {bash,zsh,fish}` prints a completion script
  (generated from the parser via shtab). New `memware[shell]` extra.
- **`memware assert -`**: batch-assert tab-separated `subject/relation/value[/source]` lines
  from stdin (blank lines and `#` comments skipped). Pairs with `beliefs --plain | cut` for
  an `$EDITOR` round-trip.
- **Richer `--help`**: per-command examples and an ENVIRONMENT/FILES/ACCESSIBILITY epilog,
  option defaults shown; duplicate global flags hidden from subcommand help.
- **XDG Base Directory support** for fresh installs: the memware home resolves to
  `$MEMWARE_HOME`, else an existing `~/.memware`, else `$XDG_DATA_HOME/memware`, else
  `~/.memware` — so an existing install is never migrated. The store path follows it.
- Man page (`docs/memware.1`), `docs/editor-integration.md` (emacs/vim/neovim/fzf recipes,
  no plugins), and `docs/accessibility.md`.

### Changed
- **Accessibility**: memware emits no colour at all (so `NO_COLOR` is honoured by
  construction and nothing depends on colour). The default human output is now labeled
  (`field : value`, one per line, empty fields skipped) — linear for screen readers. New
  `--ascii` flag and `MEMWARE_ASCII`, plus automatic fallback on a non-UTF-8 locale, replace
  the `…` elision glyph with `...`.

## [0.2.6] - 2026-09-03

### Added
- A `SessionStart` plugin hook that catches up any session whose `SessionEnd` never ran. Some
  environments force-kill Claude Code — a worktree/pane manager can `SIGKILL` the process
  group on close, and a `SIGKILL` cannot run `SessionEnd` — so its sync + backup were skipped.
  On the next start, a bare `memware sync` (new: no path = catch up the configured
  `backup.transcript_src`, default `~/.claude/projects`) plus a throttled backup run,
  **backgrounded** so startup is never delayed. Raw transcripts were durable regardless; this
  makes the *index* current without relying on a clean exit.

### Changed
- The store now sets `PRAGMA busy_timeout=5000`, so a concurrent writer waits and retries instead of failing with "database is locked" — the SessionStart catch-up, a session-end sync, and the backup cron can now overlap safely.

## [0.2.5] - 2026-09-03

### Added
- `memware setup` is now a guided one-time walkthrough for both new installs and upgrades from
  a pre-backup (pre-0.2) version: it offers to index the sessions already on disk, helps pick a
  storage-agnostic backup destination, takes a first backup, and prints the operating guidance
  (automatic session-end backups, `MEMWARE_NO_CAPTURE`, the wipe trap). `--yes` runs it
  non-interactively. A one-line hint points anyone who has never configured backups at it, and
  stops once setup has run or a destination is set.

### Fixed
- The Claude Code plugin manifest version was stuck at 0.1.1 across every release, so
  `claude plugin update` compared 0.1.1 to 0.1.1 and never reinstalled — no plugin or hook
  change (e.g. the 0.2.x automatic session-end backup hook) could reach an installed
  machine. Both manifests now track the package version, enforced by
  `tests/test_plugin_manifest.py` so it cannot silently drift again.

## [0.2.4] - 2026-09-03

### Fixed
- Recall now collapses turns whose quoted text is **byte-identical** to a higher-ranked hit, so a
  scheduled-automation prompt captured on many days takes one result slot instead of several. The
  static skip-prefix list (a schema-v2 sweep) only catches harness-injected boilerplate; a user's
  own repeated cron/system prompts are real turns it can never match. Collapse is a recall-time
  view — every turn stays in the store, a session still reads back whole, and distinct findings
  inside those sessions still surface. Deterministic, model-free; opt out with
  `collapse_duplicates=False`.


## [0.2.3] - 2026-09-03

### Fixed
- Backup retention now **promotes**: a snapshot ages forward through the 1/3/7/14-day tiers
  instead of being pruned between them. The previous rule pruned every snapshot at age ~2, so a
  daily-backup loop only ever kept a 0- and 1-day-old and never built a 3/7/14-day-old. Retention
  keeps the newest plus the oldest per age band, always has a fresh ~1-day-old, drifts within
  older tiers, prunes past the largest tier, and stays bounded (~5). Verified with a 30-day loop.


## [0.2.1] - 2026-09-03

## [0.2.0] - 2026-09-03

### Added
- Automatic backups with no scheduler: the Claude Code plugin and Hermes provider run a throttled `memware backup --if-stale` at session end (config `backup.auto`, `backup.auto_interval_hours`), so backups ride usage and survive a sleeping laptop. `--if-stale HOURS`/`--quiet` make `backup` safe to call from any hook or cron.
- Backups: `memware backup` writes a consistent snapshot (VACUUM INTO) to a storage-agnostic
  folder (Dropbox/iCloud/Drive/external disk) with tiered retention (newest + 1/3/7/14-day),
  and mirrors transcripts into `<dest>/transcripts` so they outlive the OS's 30-day cleanup.
- `memware restore --latest|--from FILE` (saves the current store aside first), `memware setup`
  (interactive backup setup), `memware config` (get/set, e.g. `backup.dest`), and `memware nuke`
  (deletes store + config + all snapshots, guarded by a typed confirmation phrase).
- Safety: a schema migration snapshots the store first when a backup dest is set, and `backfill`
  warns if a backup holds more turns than the store (the wipe-and-re-backfill trap).


## [0.1.3] - 2026-09-03

### Changed
- **Automatic, no user action:** the Claude Code parser skips harness-injected user boilerplate — a skill's `Base directory for this skill:` preamble (re-injected every session that loads the skill) and the local-command caveat — which otherwise crowd recall as near-duplicates. and a one-time store migration (schema v2) sweeps any already indexed on the next open — so backfill, ongoing capture, and existing stores are all clean without running anything. `memware prune --turns-containing TEXT` is the manual escape hatch.

### Docs
- MCP add uses `-s user` so the server is visible in every project; the default `local` scope binds it to one directory and other sessions don't see the tools.

## [0.1.2] - 2026-09-03

### Added
- `memware backfill [ROOT]` — one-time index of existing transcripts (default `~/.claude/projects`), for recall over prior sessions on a fresh install.

### Docs
- README/integrations lead with `uv tool`/`pipx` install so the plugin's hooks find the `memware` CLI on PATH (a plain venv `pip install` does not).

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

## [0.1.1] - 2026-09-03

### Added
- Multi-query recall: `memware recall Q1 Q2 …`, MCP `recall(queries=[…])`, Hermes `memware_recall(queries)` — phrasings fused by reciprocal rank so the calling agent supplies synonyms and expected values at call time.
- Persistent skip list: `~/.memware/ignore-markers.txt` and `MEMWARE_IGNORE_MARKERS` — content markers every sync honours, so transcripts predating a marker or the no-capture flag are filtered by signature.
- Evaluation guardrails: `MEMWARE_NO_CAPTURE=1` (hooks/provider/`sync --from-hook` no-op), `sync --skip-if-contains TEXT` / `--exclude GLOB`, `memware prune`, `memware-eval --corpus/--beliefs-from` clean-store builds, `memware.eval.MARKER`.
- Hermes memory-provider plugin implementing the `MemoryProvider` ABC (prefetch, non-blocking sync_turn, session flush, built-in memory mirroring, four recall/remember tools); the repo is a Claude Code plugin marketplace (`claude plugin marketplace add ericwalisko/memware`).
- `integrations/hermes/upstream/`: the Hermes provider packaged as hermes-agent's own `plugins/memory/<name>/` tree — lazy-installed dependency, profile-scoped store, `backup_paths()`, declarative desktop config schema — with tests in their layout, a docs entry draft, and a draft PR description in `docs/upstream-hermes-pr.md`. Prepared, not submitted.
- Turn hits carry `snippet`: the FTS5 window around the matched terms (the head of a long turn often lacks the answer). Default window 96 tokens; `memware recall --snippet-tokens N`.
- Release workflow that publishes to PyPI from a `v*` tag via trusted publishing
  (OIDC, no stored token). It installs the built wheel into a clean venv and refuses
  to upload when the tag and the package version disagree; `RELEASING.md` documents
  the one-time pypi.org setup and the per-release steps.

### Changed
- Stale scoring is positional: an answer is stale only when the old value appears before the current one.
- `memware-eval` mirrors the hook's subject gate for the beliefs context and reports the belief injection rate (overall and on negatives).
- `memware-eval` now scores two contexts per question: `beliefs` (what a prompt-time hook injects) and `beliefs+turns`; negatives pass when the expected value is absent rather than when the context is empty.
- The package version is read from `memware.__version__` by the build backend, so
  `memware --version` and the published distribution cannot disagree.

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
