# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **Hermes keeps memware across a venv rebuild.** The Hermes plugin declared no Python
  dependency, so Hermes' package manager left memware out of every venv it built. The venv had
  memware only because someone installed it by hand, and a rebuild dropped it. On 2026-09-24 a
  desktop-app update rebuilt the venv, and the provider reported unavailable until memware was
  reinstalled by hand. `integrations/hermes/memware/plugin.yaml` now carries
  `python_dependencies: ["memware>=0.9.0"]`. Hermes installs it from PyPI at `hermes memory
  setup` and puts it back in every venv it builds later (`hermes update`, `hermes pm install`, a
  desktop-app update). This was verified against Hermes 0.21.4+3643 in a scratch Hermes home.
  Each release raises the floor to its own version, and `tests/test_plugin_manifest.py` fails CI
  when the two differ, because Hermes keeps the memware version already in its lock while the
  floor is met.

### Changed
- **RELEASING.md has a Deploy section.** Publishing never updated the copies of memware running
  on the maintainer's Mac, and the Hermes plugin copy sat three releases behind. "Cutting a
  release" now ends with a Deploy step that updates each of those copies and checks it: the uv
  tool CLI, the Claude Code plugin, the Hermes plugin copy, the Hermes gateway's venv, and the
  gateway restart. It also covers what to do after a Hermes desktop-app update.

## [0.9.0] - 2026-09-24

### Added
- **`memware beliefs --explain ID` says why a belief is, or is not, injected (#43).** `--stale`
  explained what injection leaves out, but nothing explained what it lets in, so a stale belief
  still reaching every prompt could only be diagnosed by driving the classifier's private
  predicates by hand. `--explain` prints the class or `durable`, the test that decided it (the
  rule that fired, or the check each class failed), the qualifier behind a veto and whether it
  came from the subject or the relation, each exemption (reliability above 0.5, a source that is
  not a `memware:session/` pointer, a confirmation), the manifest check and the
  `inject.volatile_days` window, and `injected : yes` or `no`; `--json` too. `--explain
  --subject S --relation R --value V` judges a triple that is not in the ledger and opens no
  store. It reads the store through a handle SQLite refuses to write through. `--stale`, the
  prompt hook, the digest and `--explain` all read one function, `Gate.explain`, and derive's
  gate reads the same classification, so they cannot disagree.

### Fixed
- **An incidental word no longer turns a measurement durable (#42).** The qualifier veto split
  `scheduled_export` on the underscore and found `scheduled`, so `scheduled_export | null rate |
  41% null` was injected as a durable fact. An identifier in the subject is now a name: a
  qualifier inside it counts only when the identifier names a setting, by ending in the
  qualifier (`export-schedule`, `ruff-pin`), holding a bound (`min_coverage`) or joining a
  qualifier to what would be measured (`max_rows`, `page_size`). A whole-word qualifier still
  vetoes every rule, the exact measures included (`required ci | coverage | 90%` stays durable).
  An "N of M" figure finds its counted noun in the subject too (`appointment rows | eligible and
  exported | 2,454 of 10,346`), and a requirement word in the relation makes it a rule
  (`release gate tests | must pass | 3 of 3`). A count qualified in the relation (`scheduled row
  count`, `spec-required row count`) stays durable: a target, not a reading.
- **`memware exclude --apply` removes what it un-indexes from the store file.** It deleted the
  matching turns and cursors but never scrubbed, so the search index kept each removed turn's words,
  lowercased, on its pages. A synthetic store synced with a transcript holding a token, then
  `exclude --add '*privateproj*' --apply`, left 0 turns and 0 `fts5vocab` terms but 2
  case-insensitive copies of the token in the file (4 with `secure_delete` off). `memware scan`
  reported the term held and exited 1, and a backup taken afterwards copied it. Only a later `prune
  --scrub` removed it. Now, whenever it un-indexes anything, `exclude --apply` runs the same scrub
  as `prune --apply` (`memware.ingest.unindex_sources`, which calls the prune's own scrub step):
  both search indexes merged, one a merge leaves holding deleted terms rebuilt, `VACUUM`, the
  write-ahead log emptied without waiting for a reader while the lock is held, each step announced
  on stderr. Then it checks the index pages for terms no live row holds (`left in the search index`,
  `left_in_index` and `index_check` in `--json`). The line says `nothing` only when the scrub
  finished and emptied the log; otherwise it says `not checked` and why. It names the backup
  destination whose earlier snapshots and mirrored transcripts may still hold the text. It exits 1
  with the command to finish when the scrub did not finish, the log could not be emptied, or the
  index still holds such terms or could not be read. `--json` adds `store_scrubbed`, `scrub_error`,
  `left_in_index`, `index_check` and `backup_dest`. Beliefs are still left in place, as before. **A
  store an exclusion was applied to with 0.8.0 or earlier still holds that text: run `memware prune
  --scrub` once.** So does a store whose pattern was added with `memware config capture.exclude` and
  then applied by a sync, which un-indexes without scrubbing.
- **`memware exclude --add` refuses a pattern written as a path that matches nothing.** Claude Code
  names a project's transcript directory after its whole path with every character but a letter or
  digit a dash (`-Users-me-Developer-olivia-career`), so `'*/olivia-career/*'` matched nothing.
  The dry run printed `transcripts : 0` and a hint that `*/name/*` names a directory, and `--apply`
  saved the pattern and reported success. Now a new pattern that matches no transcript on disk and
  no indexed source says so. When it holds a `/`, `--apply` refuses it, writes nothing and exits 2
  (`refused` in `--json`), and `--force` adds it anyway, for a project that has not run yet. The
  output lists the forms built from the pattern's last name that match something, each with its
  transcript and indexed-source counts (`suggestions` in `--json`). `'*olivia-career*'` comes
  first, because it keeps the project out along with its subdirectories and worktrees, which
  Claude Code keeps in directories of their own
  (`-Users-me-Developer-olivia-career--claude-worktrees-feat`). `'*-olivia-career/*'` comes second,
  and the output says it leaves those sessions indexed. Adding a pattern already in
  `capture.exclude` changes nothing and exits 0.

### Changed
- **Two narrow status rules, from beliefs that stayed injected after going stale.** A relation
  ending in status (`memware PR #31 | ci status | green`, `connection status | connected`) is a
  status when its value is a status word or an instance id (`#31`, `t_…`) is named; a subject
  noun such as job or card is not enough (`backup job | exit status | non-zero on failure` stays
  durable), and a compound "… state" is a design term (`sync indicator | error state | red`). A
  relation naming a finding or a defect (`known issue`, `open issue`, `must-fix issue`,
  `should-fix issue`, `blocker`) is a status unless the value points at where it is tracked or
  states a by-design limitation or a workaround; this also catches `memware 0.4.0 | known issue`
  outside its project, where the manifest rule cannot. Derive's prompt now rejects "a defect, a
  review finding or an open issue: it stops being true when someone fixes it", and says that a
  fix, its cause, a workaround or a by-design limitation would not need re-checking. The labeled
  corpus grows from 115 to 160 cases with no durable case left out; four cases stay missed on
  purpose: `memware sync at 50k turns | latency | 3.7 s` and `personal-os board | open cards
  count | 55`, too close to durable configuration for a word rule, and `scheduled_user_sync |
  row count` and `scheduled_test_run | status`, whose identifiers read like a setting's name.
- `prune --glob --apply` runs the same index check after its scrub and prints `left in the search
  index` (`left_in_index` and `index_check` in `--json`); an index still holding terms of deleted
  rows, or one that could not be read, exits 1, as `exclude --apply` does.
- `docs/keeping-memory-clean.md` explains Claude Code's dash-encoded project directory names beside
  the `'*/-Users-me-gen-runs/*'` example, including a worktree's directory, and compares the
  `*name*` form, which keeps a project out, with the narrower `*-name/*`. It says what `exclude
  --apply` now scrubs, when `left in the search index` can say `nothing`, and that a pattern a sync
  applies, or one applied with 0.8.0 or earlier, needs a `memware prune --scrub`. `docs/memware.1`,
  the README's exclusion example and `memware exclude --help` follow.

## [0.8.0] - 2026-09-24

### Added
- **An optional relevance filter for prompt-time injection, off by default.** The prompt hook and
  the Hermes provider's `prefetch` pick beliefs by keyword, so a prompt about an incident report
  also got a weekly report's file path. With `memware config relevance.mode filter`, memware asks
  TypeSafe's System One model (`jev-1.13.0`, pinned) one yes/no question per candidate, all in one
  request, and injects the candidates at or above `relevance.threshold`, at most k. `shadow` makes
  the same call, logs each (prompt, candidate) pair to `relevance-log.jsonl` for calibration, and
  injects what `off` does. When it is on, the prompt (up to 2,000 characters) and up to 20
  candidate facts go to `api.typesafe.ai`, and nothing else does. A task notification, a hook
  inside a subagent and a session memware keeps out of its store are never sent. With the filter
  off, memware reads no key, opens no socket and prints the same bytes as before. It fails open
  on a missing key, a timeout, an HTTP error, a redirect or a malformed reply, with one attempt
  under a 1.5 s deadline. With a full pool of 20 candidates the hook took 553 ms p50 and 753 ms
  p95, against 84 ms and 99 ms off. It uses the standard library only, with no new dependency.
  `memware config` refuses a bad `relevance.*` value and says what switching the filter on sends.
  `memware nuke` removes the log and `relevance-usage.jsonl`.

## [0.7.1] - 2026-09-23

### Fixed
- **A prune's dry run stays readable for a text of one to three letters.** The output guard that
  keeps a prune from printing its text also matched those letters inside memware's own labels and
  inside the `[removed]` marker, so a dry run for `e` printed `b[removed]li[removed]fs to
  r[removed]dact`. Labels and the marker are now left alone; the text is still withheld
  everywhere else.
- **A redacted belief's key prints with the `[removed]` marker intact.** Key normalization strips
  punctuation at the edges of a field, so a subject beginning with the marker printed as
  `removed] vault|owner`. The key is still made from the already-withheld fields, so no form of
  the text can reach it through normalization.

## [0.7.0] - 2026-09-17

### Added
- **`memware scan` counts every place a value is still stored**
  ([#37](https://github.com/ericwalisko/memware/issues/37)). `prune --containing` reads only
  indexed transcripts, so a transcript kept out of the index, by `capture.exclude` above all, was
  one no memware command could look inside, and verifying a removal meant leaving the tool. `scan`
  walks the transcript source on disk and reports each file holding the value: how many times,
  whether it is indexed, and if not why (`capture.exclude`, the no-capture list, an ignore
  marker, or not synced yet). Every file is read whole, and a value escaped inside a JSON string
  counts too. It checks the store file and its `-wal` for the value's bytes, as given and in any
  case, counts the turns, beliefs and other rows holding it, and reads the search index's own
  pages for the value's terms, including those only a deleted row left, which SQLite's
  vocabulary view does not show. It also reads the `pre-restore` copies beside the store, and with
  `--backups` the mirrored transcripts and snapshot files in the backup destination. A path it
  cannot read is listed with the reason. It is read-only: the store's bytes are counted before it
  is opened, and it is opened so that no write-ahead log is ever checkpointed, and a symlinked
  store is followed to its log. It prints paths and counts, never the value or the text around
  it. The value comes from a prompt that does not echo it, `--value-file`, or stdin (`-`), so it
  stays out of `ps`, shell history and, run outside Claude Code, any transcript. `--json` is
  supported. Exit status: 0 nothing found, 1 found, 2 nothing found but a path could not be read.
- **`memware prune --scrub`** rewrites the store file, removing nothing: the command a prune prints
  when its scrub could not finish.
- **`memware beliefs --stale`** lists the current beliefs injection leaves out, each with its
  reason (`measurement`, `moving_version`, `status`, `contradicted`, `older_version`) and why
  (`pyproject.toml says 0.6.1`). `--cwd DIR` names the project whose manifest is checked.
- **`memware beliefs retract` takes `--stale` or belief ids**, as a dry run unless `--apply`, as
  `--orphaned` does. Rows are kept and each retraction records its reason. Neither reopens what
  a retracted belief had superseded, because that value is older still. An id that names a
  belief no longer current is refused with the reason and nothing is written: it reaches no
  prompt already, and retracting it would move the end of its interval.
- **`memware stats` counts the beliefs injection leaves out, by reason** (`injection.left_out`
  under `--json`), with the window and the manifest versions it read.
- **An upgrading user is told once.** The session-start notice says how many beliefs are no
  longer injected and names `memware beliefs --stale`. The marker is a row in the store's own
  `notice` table, so a memware home that takes no write cannot repeat it or keep it from
  firing. Once it is recorded, a session start only reads it, taking no lock; the one write
  waits at most a quarter second for another writer. It never creates a store.
- **`memware config inject.volatile_days`** refuses a value that is not a number of days, 0 or
  more (`7d`, `-3`), exits 2 and writes nothing.

### Fixed
- **An applied `memware prune` removes the text from the store file, not only from every
  query** ([#36](https://github.com/ericwalisko/memware/issues/36)). A deleted turn stayed
  readable in the file: SQLite frees a deleted row's page without zeroing it unless
  `secure_delete` is on, and builds disagree on that default (Homebrew's Python leaves it off). A
  full-text index keeps a deleted row's words on its pages, lowercased, until it merges, which
  happens on every build and which a case-sensitive search of the file cannot see. A backup
  snapshot taken after the prune copied those index pages, and the `-wal` file kept older copies
  of pages while another process held the store open. And a prune that retracted a belief wrote
  its own command line, text included, into the retraction's reason. Now the store turns
  `secure_delete` on for every connection, a retraction's reason reads `(value withheld)` where
  the text was (and a reason an older prune wrote is rewritten), and an applied prune scrubs the
  file when it removed anything or copies of the text are left that no row accounts for: it
  merges both search indexes, rebuilds one a merge left holding a deleted row's terms, runs
  `VACUUM`, and empties the write-ahead log. It then checks the file and prints what it still
  holds. It exits 1, naming the files and counts and printing `memware prune --scrub` to finish,
  when copies remain or the scrub failed (a lock, a full disk); the removal itself stays
  committed. On a 50,000-turn store a matching prune takes 3–4 s instead of 1.2 s, and one
  matching nothing 1.1 s instead of 0.65 s. Standard error names each scrub step, and says where
  copies may remain that memware never changes: backups made before now and the transcript files.
  `--json` adds `beliefs_redacted`, `beliefs_retracted_by_redaction`,
  `confirmation_sources_redacted`, `retraction_reasons_redacted`, `store_scrubbed`, `scrub_error`,
  `left_in_store` and `backup_dest`.
- **No prune output prints the text it removes.** The retraction a prune cascades into listed the
  beliefs it retracts as they were, so a derived `staging api key = <secret>` printed the secret,
  in the dry run and in `--apply`, in every view. Every field of those records, their keys, the
  notes and any error now read `[removed]` where the text was; `--json` withholds it in values
  only, so the JSON stays valid.
- **A hook's sync gives up quietly instead of waiting a minute.** `memware sync --from-hook` (the
  PreCompact hook runs it in the foreground with a 30 s timeout) waits 5 s for the lock and exits
  0 with nothing printed when it does not get it; the next sync catches up from each cursor.
- **A prune never prints or records its text.** The notes a selector that matched nothing prints
  said `no turn contains 'VALUE'`; they now say `the text`. A text selector written without its
  text asks for it without echoing it, or reads `--value-file` or stdin. A value is one line:
  trailing line breaks are dropped and one that still holds a line break is refused, so a value
  file with a stray blank line cannot make `scan` report a clean store.
- **Writes that must land wait out a long write; recall never does.** A store connection waits
  up to 60 s for another's write lock by default (was 5 s), and its caller can choose a shorter
  wait. On a 150,000-turn store the prune and its scrub held the lock past 5 s, so a hook's sync
  failed with `database is locked` and a Hermes memory write was lost; both now wait and land.
  Recording a use (Hermes prefetch, MCP recall, `memware recall`) waits at most 250 ms and is
  skipped when the lock is not free, and the scrub never waits for a reader while holding the
  lock: with a reader held during a 150,000-turn prune, Hermes prefetch took at most 0.33 s
  (10.6 s before).
- **A hook never waits out a schema upgrade.** The first open after an upgrade that adds a table
  (this release adds `notice` and `confirmation`) takes the write lock. From the prompt hook, the
  session-start digest or the notice, with another writer holding the lock, that open waited 12 s,
  past the hooks' 5 s and 10 s timeouts. A hook now waits 250 ms, says nothing that time, and the
  next open, a sync's or any command's, adds the tables. The notice's own write and a person's
  confirmation follow the same rule for writes nothing depends on.
- **A prune does not report a live row's search term as a copy it failed to remove.** Pruning
  `hunter2` while a turn says `Hunter2` left the term `hunter2` on an index page for that turn;
  the check now counts turns and beliefs that hold the text in another case and reports them,
  exits 0, and does not scrub again on later prunes.
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
- **The Hermes provider applies the same rule.** Its `prefetch` leaves out a belief memware
  marks `volatile` (honouring `inject.volatile_days`) and its header no longer claims facts are
  currently valid. The staged upstream copy gets the equivalent change and still works against a
  memware that predates the mark. Neither has a project directory, so the manifest rules apply
  only to the Claude Code hooks.

### Changed
- **The prompt hook and the session-start digest stop injecting beliefs that were true when
  recorded and are wrong now.** A derived belief closes only when a later derive supersedes the
  same key, which rarely happens, so `memware main branch current version: 0.4.0`, `built memware
  wheel version: 0.5.0` and `memware test suite test count: 91 tests` reached every session under
  "Known facts (currently valid…)" long after the repository moved on. None of them is an orphan,
  so `retract --orphaned` and `prune` never touched them. Both unsolicited readers now leave out:
  - a derived belief `memware.volatile` classifies, **unambiguously**, as a **measurement** (a
    count or total of rows, records, tests, files, lines, commits, duplicates, accounts, users or
    downloads; a magnitude or a comma-grouped number of 1,000 or more beside one; an "N of M"
    over one; a relation that is exactly progress, coverage or null rate), a **moving version**
    (a version called current, latest, built, installed, deployed, released or on main) or a
    **status** (a relation that is exactly status, state or progress, with a status word for a
    value or an instance for a subject: `card t_cd03d14d status: review`, `graph_health scan
    status: …`). Precision over recall: hiding a durable fact silently removes something someone
    relied on, while a stale belief that slips through is the old behaviour and `memware beliefs
    retract ID` removes it. So a qualifier (slo, sla, target, threshold, budget, commitment,
    fail under, min, max, limit, default, initial, final, required, desired, every, schedule,
    check, and their like) always means durable, and anything in doubt is durable:
    `api p99 latency slo: 200ms`, `ci status check: required`, `order state machine final state:
    completed`, `main branch python version: 3.12`. `tests/data/volatility_cases.jsonl` holds 115
    labeled cases: none of the 69 durable ones is left out, and the 13 volatile ones the narrow
    rules miss are kept there, marked, so the tradeoff is explicit. The derive prompt carries
    the fuzzy judgment for new beliefs, where the model sees the excerpt. It is left out by
    default; `memware config inject.volatile_days N` injects one while it is younger than N days.
    The default is 0 (never) because the reported version belief was one day old and already
    wrong, so no multi-day window would have kept it out;
  - inside a project whose manifest declares a version (`pyproject.toml`, including a hatch
    `[tool.hatch.version] path`; `package.json`; `Cargo.toml`), a belief about the project's own
    version that differs from it (**contradicted**), and a belief naming an older version of
    the project beside its name, such as `memware 0.4.0 config format` (**older version**).
    A declared version is checked only against beliefs whose subject names the package that
    declares it, never another package's; a version a build tool computes (setuptools-scm) and
    a `0.0.0` placeholder are never checked against. Only the project root's manifests are
    read, not a monorepo's nested packages.
  A person stating a fact is a decision to keep it: a belief with reliability above derive's
  0.5, or a source that is not a `memware:session/` pointer (`remember`, `memware assert`), is
  exempt from all of it. So is a derived belief a person has since confirmed, by asserting the
  same value (`memware assert`, `remember`) or approving it in `memware review`: that is how a
  fact the gate left out is kept. The confirmation is a row beside the belief; the belief row
  keeps its session source. Nothing is deleted or hidden: a left-out belief stays in `memware
  beliefs`, `recall` and the MCP tools, which now mark it with `volatile` (its class). The
  classification is regex and word lists, no model and no network, because the prompt hook runs
  it on every prompt.
- **An applied `memware prune` with a text selector redacts that text in beliefs.** A belief that
  quoted a pasted secret kept it: prune never rewrote a belief row. Now every belief whose subject,
  relation, value or free-text source holds the text, matched as the selector matches turns, has
  it replaced with `[removed]`, in any status (committed, candidate, rejected, retracted,
  superseded) and whether derive filed it or a person stated it, because removing a secret
  outranks the rule that a person's belief is never retracted. The key follows a rewritten subject
  or relation. A committed belief is also retracted, with the reason `memware prune: text redacted
  (value withheld)`, by its status alone: its id, `valid_from`, `valid_to` and supersession links
  stay, and no older value is reopened. A belief already retracted keeps its retraction. A
  confirmation's source that quoted the text is redacted too. A source memware wrote itself
  (derive's `memware:session/<id>/turn/<n>` pointer, the Hermes provider's `hermes built-in memory
  (…)`, an approval's `review #N approved`) is provenance and is never matched or rewritten. An
  open review whose candidate or incumbent is redacted is closed with the decision `redacted`, and
  approving a redacted or retracted candidate is refused. No belief row is deleted, and none is
  merged, even one redaction makes identical to another. The dry run lists the beliefs it would
  redact by id. An `--apply` whose redaction would rewrite more than 20 beliefs, or any belief for
  a text shorter than 6 characters, is refused whole: nothing is written, the counts are printed,
  it exits 2, and `--allow-broad-redaction` applies it anyway. On a synthetic ledger of 330
  beliefs, `api` would have redacted 157 and `memware` 310, 300 of them by rewriting derive's
  session pointers.
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
