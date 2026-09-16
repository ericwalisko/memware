# Keeping unwanted content out of memware

memware indexes transcripts, and some transcripts should never become memory —
evaluation and benchmark runs above all (each contains the questions and the
answers, so indexing them lets the system "remember" its own test), plus
throwaway experiments and anything you simply do not want recalled later.

There are three layers. The switch stops *new* runs cheaply, the path exclusion
stops a generator whose author forgot the switch, and the filter catches everything
else — including runs that happened *before* you added any marker, which the switch
cannot help with retroactively. They sit side by side; [how they fit
together](#how-the-three-layers-fit-together) is at the end of Layer 3.

## Layer 1 — the switch (prevents capture at the source)

Set `MEMWARE_NO_CAPTURE=1` in the environment of a run you do not want indexed.

```bash
MEMWARE_NO_CAPTURE=1 claude -p "…"          # neither indexed nor mirrored to a backup
MEMWARE_NO_CAPTURE=1 my-eval-harness.sh      # nor is any Claude Code session it launches
```

The variable exists only in that run's environment. The memware processes that would otherwise
pick the transcript up never see it: the `SessionStart` catch-up that the *next* session runs, a
`memware backfill`, and the backup mirror. So the Claude Code plugin's hooks write the decision
down. Every hook that runs under the variable adds the session's transcript path to
`<home>/no-capture.txt`: `notice` and `digest` at session start, `context` on each prompt, and
`sync` at compaction and session end. Recording happens at session start, so a session that is
later force-killed is still covered. Every sync, including the catch-up and a backfill, skips a
listed transcript and un-indexes it if an earlier sync indexed it. `memware backup` never mirrors
it. Both treat the transcripts of the session's subagents the same way: Claude Code keeps them in
`<session>/subagents/`, beside `<session>.jsonl`. The list holds paths only; delete a line to let that session be indexed after all, and
`memware nuke` deletes the file with everything else.

The Hermes provider reads the variable itself and captures nothing under it: no session file, no
indexed turn, no mirrored memory write. Its tools still answer.

### What the switch cannot do

- **It needs a memware hook to run in the session.** With the plugin absent, hooks disabled, or
  user settings excluded (`--setting-sources project` drops the plugin with them), nothing is
  recorded, and only a marker (Layer 2) can identify the transcript.
- **It is not retroactive.** In memware 0.4.0 and earlier the variable left no trace outside its
  own session: the catch-up indexed such sessions (since 0.2.6), `backfill` indexed them, and the
  mirror copied them (since 0.2.0). Those sessions cannot be identified now. If you know which
  they were, un-index them with `memware prune --glob` or `--containing` and `--apply`, and delete their copies
  from `<dest>/transcripts` by hand. memware never deletes from a backup destination; `memware
  backup` lists the copies it does recognise (listed or marked transcripts still on disk) under
  `transcripts_left_in_backup`.
- **Claude Code still writes the transcript.** It lands in `~/.claude/projects` whatever memware
  does. For a headless run, `claude -p --no-session-persistence` writes no transcript at all.

It is the cheapest option **for runs you control going forward**. It does nothing for a run that
forgets to set it.

## Layer 2 — the content filter (catches everything, retroactively too)

Put a stable marker string in every prompt your evaluation sends
(memware uses `[memware-eval]`), then list markers in
`~/.memware/ignore-markers.txt` — one per line, `#` comments allowed:

```text
# any transcript whose head contains one of these is never indexed or mirrored
[memware-eval]
Answer briefly using only what you know     # an older harness's prompt, no marker of its own
```

Every `sync` — the hooks, the provider, `memware sync`, a full-tree backfill,
the nightly derive lane — checks this list (and the `MEMWARE_IGNORE_MARKERS`
env var, same format) and skips any file whose head contains a listed string.
The backup mirror skips those files too and counts them (`transcripts_skipped_marker`).
A marker is matched per file: a subagent's transcript (under `<session>/subagents/`) starts with
the subagent's own prompt, so it is skipped only when that prompt carries the marker as well.
This is the layer that handles the awkward case: **runs that predate the switch
or the marker.** They carry no flag, so only their *content* can identify them —
list a phrase unique to that harness's prompts and they are filtered forever,
including if a backfill re-scans the whole transcript tree.

### Clean up what already slipped in

If eval runs were indexed before you set any of this up:

```bash
memware prune --containing "[memware-eval]"          # dry run: what would go, nothing written
memware prune --containing "[memware-eval]" --apply  # by content marker
memware prune --containing "Answer briefly using only what you know" --apply
memware prune --glob "*/eval-runs/*" --apply         # or by path
memware stats                                        # confirm
```

`prune` un-indexes matching sources (turns and their cursor). A later sync will
not bring them back as long as the marker is in the ignore list. Without `--apply` it
writes nothing and prints what it would do, beliefs included (below).

`--containing` reads every transcript file to the end, so it finds a marker however deep in a
long session it sits. It cannot read a transcript whose file is gone, and it says how many it
skipped; their turns are still indexed, and `--turns-containing` reaches them. A selector that
matches nothing prints what it searched, so a `0` is never silent.

### Remove a value pasted into a session you keep

A token or password pasted mid-conversation sits inside turns you otherwise want. Select the
turns that hold it, not the whole transcript:

```bash
memware prune --turns-containing "the-pasted-value"          # dry run: how many turns hold it
memware prune --turns-containing "the-pasted-value" --apply
```

`--turns-containing` matches the text anywhere in a turn, literally and case-sensitively. Copies
already mirrored to a backup folder are not touched; delete those by hand.

### Retract the beliefs those runs left behind

A run that was indexed may also have been derived: `memware derive` files the facts it finds
as beliefs, each citing its evidence as `memware:session/<id>/turn/<n>`. Un-indexing the run
removes the evidence, not the belief, and the belief would go on reaching prompts as a known
fact. So `prune` also retracts every committed belief whose cited session it leaves with no
turn. The dry run lists, before anything is written:

- **retract**: the beliefs derived from those sessions;
- **reopen**: a value one of them had superseded, which becomes current again (or **relink**,
  when a later belief had already superseded the retracted one: the older value then closes
  where that later one starts);
- **keep**: beliefs whose source names one of the sessions but is not a session pointer. A
  person stated those through `remember` or `assert`, and memware never retracts them.

A retracted belief is closed at its own start (`status` `retracted`, `valid_to` equal to
`valid_from`). It leaves `recall`, the prompt hook, the digest, `memware beliefs` and the
`beliefs current` count, and it stays in the history of its key (`memware beliefs SUBJECT
RELATION`) with when and why it was retracted. No belief row is deleted.

Runs un-indexed some other way leave their beliefs behind: an earlier `memware prune`, a sync
that skipped a transcript it had indexed before (because of a marker, the no-capture list or a
`capture.exclude` pattern), or `memware exclude --apply`. A sync never changes a belief. `memware stats` counts these beliefs
(`beliefs citing an unindexed session`), and the one-shot has the same dry-run shape:

```bash
memware beliefs retract --orphaned           # dry run: what would be retracted and reopened
memware beliefs retract --orphaned --apply   # retract them
```

**A dangling citation is not the same thing.** Re-indexing a transcript (a `memware sync` that
re-reads a file whose turns were re-parsed) can renumber a session's turn ids while the session
stays indexed. A belief still citing the old id then points at a turn that no longer exists,
even though its evidence — the session itself — is still in the store. `memware stats` counts
these apart, as `beliefs with a stale turn citation`, and does not offer `retract --orphaned`
for them: retracting would discard a belief whose evidence is still present, just under a
different turn id. There is no repair command for this yet.

## Layer 3 — path exclusions (the machine remembers)

The switch lives in a run's environment, so a script that forgets to set it is captured. A marker
lives in the transcript, so a harness that never writes one is captured. `capture.exclude` lives
in the machine's config: a list of path globs. A transcript whose resolved path matches one is
never indexed and never mirrored, whoever started the run and whatever it sent.

```bash
memware exclude                                          # each pattern: matches on disk and in the index
memware exclude --add '*/-Users-me-gen-runs/*'           # dry run: what it matches, what it would un-index
memware exclude --add '*/-Users-me-gen-runs/*' --apply   # write it, and un-index what it matches
memware exclude --remove '*/-Users-me-gen-runs/*' --apply
```

Nothing changes without `--apply`, and the dry run reads the store read-only. A pattern is matched
with shell-style wildcards against the whole resolved transcript path, as `memware prune --glob`
matches, so `*` crosses `/`, and a leading `~` is expanded. Claude Code keeps one transcript
directory per working directory under `~/.claude/projects`, named after the path with each `/`
turned into `-`. So `*/-Users-me-gen-runs/*` names every session started in `/Users/me/gen-runs`,
and their subagents' transcripts in `<session>/subagents/` with them. A pattern that begins with
`-` is passed as `--add=-Users-…`.

Every sync skips a matching transcript and un-indexes it if an earlier sync indexed it, exactly as
it does for a marker: the hooks, the `SessionStart` catch-up, `memware sync`, `backfill`, `setup`
and the Hermes provider. `memware backup` never mirrors it and counts it under
`transcripts_skipped_glob`. A copy an earlier run already made is listed under
`transcripts_left_in_backup`; memware never deletes from the destination, so remove it by hand.
`memware exclude --add … --apply` also un-indexes matching sources whose transcript is no longer on
disk, which no sync would walk to again. Like a sync, it leaves the beliefs derived from those
sessions in place and says how many beliefs now cite an unindexed session; `memware beliefs
retract --orphaned` retracts them (see [Layer 2](#retract-the-beliefs-those-runs-left-behind)). An edit to `config.json` by hand, or through `memware
config capture.exclude`, takes effect at the next sync, for the transcripts still on disk.
Removing a pattern re-indexes nothing by itself: the next sync indexes the transcripts it hid,
if they are still on disk.

**Run a generator from a working directory of its own.** A pattern can tell sessions apart only by
where they ran. A pipeline whose steps start Claude Code in the directory you work in yourself —
say, distill, examiner and judge steps that all run from the root of your notes repository —
shares that transcript directory with your interactive sessions, and a pattern for it would
exclude your own work as well. `cd` the generator into a directory nothing else uses before it
starts each session, and a pattern can name it alone.

**Watch the share.** An over-broad pattern hides real work without a sound. `memware exclude`
prints, for each pattern, the transcripts on disk and the indexed sources it matches, and the share
of all transcripts on disk the patterns hide together. `memware stats` shows that share whenever a
pattern is set. Both commands, and `memware backup`, print a warning when the patterns hide half or
more of the transcripts on disk.

### How the three layers fit together

Side by side, not instead of each other: a transcript is excluded when any layer names it.

| layer | lives in | names a run by | covers runs from before it was set | needs |
|---|---|---|---|---|
| no-capture list | `<home>/no-capture.txt`, written by the hooks | the session's own transcript path | no | `MEMWARE_NO_CAPTURE=1` and a memware hook in the session |
| path exclusion | `capture.exclude` in `<home>/config.json` | the directory the session ran in | yes | the generator in a working directory of its own |
| skip marker | `<home>/ignore-markers.txt` or `MEMWARE_IGNORE_MARKERS` | text in the transcript's head | yes | the text in the run's prompts |

`memware backup` counts a transcript under the first layer that names it, in that order:
`transcripts_skipped_no_capture`, `transcripts_skipped_glob`, `transcripts_skipped_marker`. The
`--exclude GLOB` flag of `memware sync` and `backfill` is a different thing: it skips for that
one call and un-indexes nothing.

## Writing evaluations that don't poison the store

1. Set `MEMWARE_NO_CAPTURE=1` for the whole run **and** put `[memware-eval]`
   (or your own marker) in every prompt — belt and braces. Headless runs should
   also pass `--no-session-persistence`, so there is no transcript to leak.
2. Judge retrieval against a store that excludes the run:
   `memware-eval --corpus ~/.claude/projects --beliefs-from ~/.memware/memware.db`
   rebuilds a scratch store skipping marked transcripts and attaches your live
   belief ledger. `--also-skip TEXT` adds markers for older harnesses.
3. Author **negative** questions (ones that should return nothing) from words
   you have never typed in an indexed session — a dictionary and a random seed.
   The moment "zebra habitat" is written into a chat that gets indexed, it stops
   being a negative.

## Recurring automation prompts (the collapse edge)

Recall collapses hits whose quoted text is **byte-identical**, so a scheduled prompt captured
the same way every run takes a single result slot instead of many. The match is deliberately
exact: fuzzy or normalised matching would risk merging genuinely different facts (`port 8443`
vs `port 9000`, `v1` vs `v2`) into one and hiding real evidence — the opposite of the point.

The edge it does **not** cover: a prompt that interpolates a **date, run number, or timestamp**
produces *near*-identical turns that are not byte-identical, so each run survives and can crowd
recall. Handle it at capture, not with collapse — the text *before* the varying token is a stable
content signature:

```bash
# one-shot: drop every copy already indexed (the stable prefix matches them all, dates and all)
memware prune --turns-starting-with "You are the NIGHTLY DRIFT SCAN" --apply

# ongoing: if that prompt begins its own automation sessions (a cron that opens a fresh
# Claude session), skip the whole session at every sync:
echo "You are the NIGHTLY DRIFT SCAN" >> ~/.memware/ignore-markers.txt
```

`ignore-markers.txt` matches the **head** of a transcript, so it skips a session whose first
turn is the recurring prompt — the usual shape for a cron. If the prompt is embedded *mid*-session
in work you otherwise keep, there is no ongoing per-turn skip yet: re-run `prune --turns-starting-with`
periodically (its stable prefix keeps catching the dated variants). Use `--turns-starting-with`
rather than `--turns-containing` here: a substring match would also remove every turn that quotes
the prompt. Static recurring prompts need none of this — they collapse cleanly on their own.

## What derive reads (not a capture layer)

The layers above decide what enters the store. `derive.sources` decides something narrower:
which indexed turns `memware derive` turns into beliefs. Its default, `interactive`, skips turns
Claude Code labels as headless (`sdk-cli` for `claude -p`, `sdk-ts` and `sdk-py` for the Agent
SDKs). Those turns are still indexed, recalled and mirrored. `memware config derive.sources all`
lets derive read them too; [docs/scheduling.md](scheduling.md#which-sessions-it-reads) has the
details.

It does not replace the no-capture switch or a marker, and it cannot. It reads a label for *how*
a session started, not *what* the session was: an agent launched in a terminal without `-p` is
`cli`, and so are the subagents of an interactive session, while a lane whose `claude -p`
sessions hold real decisions is `sdk-cli`. That is why it is a default with an opt-in rather
than an exclusion. The layers above name a run by what it is, and they keep it out of recall and
the backup mirror as well as the ledger. Mark an evaluation and set the switch whatever this
setting says. A turn with no entrypoint is read as interactive, so the setting never hides
evidence it cannot label.

`memware stats` shows sessions, turns and beliefs by entrypoint and the project directories
holding the most sessions. A generator that slipped past the layers above shows up there as a
directory with an outsized share, which is the cue to mark it, list it or prune it.

## Quick reference

| goal | do this |
|---|---|
| never index or mirror this run | `MEMWARE_NO_CAPTURE=1` in its environment (a memware hook must run in the session) |
| never write the transcript at all | `claude -p --no-session-persistence` |
| never index or mirror a generator, whatever its environment | run it from its own directory; `memware exclude --add '*/<project-dir>/*'`, then again with `--apply` |
| never index or mirror anything matching a phrase | add the phrase to `~/.memware/ignore-markers.txt` |
| remove already-indexed runs | `memware prune --containing TEXT` / `--glob GLOB`, then again with `--apply` |
| retract beliefs whose session is gone | `memware beliefs retract --orphaned`, then again with `--apply` |
| remove runs already mirrored | delete them from `<dest>/transcripts` by hand; `memware backup` lists those it recognises |
| remove a value pasted into a session you keep | `memware prune --turns-containing VALUE`, then again with `--apply` |
| tame a recurring/dated automation prompt | `prune --turns-starting-with PREFIX --apply`; add PREFIX to `ignore-markers.txt` if it heads its own sessions |
| evaluate without self-contamination | `memware-eval --corpus … --beliefs-from …` |
| keep headless runs out of the ledger, in recall | the default (`derive.sources interactive`); `memware config derive.sources all` reads them |
| see which generator wrote the store | `memware stats`: sessions, turns and beliefs by entrypoint, top project directories |
