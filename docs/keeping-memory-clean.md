# Keeping unwanted content out of memware

memware indexes transcripts, and some transcripts should never become memory —
evaluation and benchmark runs above all (each contains the questions and the
answers, so indexing them lets the system "remember" its own test), plus
throwaway experiments and anything you simply do not want recalled later.

There are two layers. Use both: the switch stops *new* runs cheaply, the filter
catches everything else — including runs that happened *before* you added any
marker, which the switch cannot help with retroactively.

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
  they were, un-index them with `memware prune --glob` or `--containing`, and delete their copies
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
memware prune --containing "[memware-eval]"          # by content marker
memware prune --containing "Answer briefly using only what you know"
memware prune --glob "*/eval-runs/*"                 # or by path
memware stats                                        # confirm
```

`prune` un-indexes matching sources (turns and their cursor). A later sync will
not bring them back as long as the marker is in the ignore list.

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
memware prune --turns-containing "You are the NIGHTLY DRIFT SCAN"

# ongoing: if that prompt begins its own automation sessions (a cron that opens a fresh
# Claude session), skip the whole session at every sync:
echo "You are the NIGHTLY DRIFT SCAN" >> ~/.memware/ignore-markers.txt
```

`ignore-markers.txt` matches the **head** of a transcript, so it skips a session whose first
turn is the recurring prompt — the usual shape for a cron. If the prompt is embedded *mid*-session
in work you otherwise keep, there is no ongoing per-turn skip yet: re-run `prune --turns-containing`
periodically (its stable prefix keeps catching the dated variants). Static recurring prompts need
none of this — they collapse cleanly on their own.

## Quick reference

| goal | do this |
|---|---|
| never index or mirror this run | `MEMWARE_NO_CAPTURE=1` in its environment (a memware hook must run in the session) |
| never write the transcript at all | `claude -p --no-session-persistence` |
| never index or mirror anything matching a phrase | add the phrase to `~/.memware/ignore-markers.txt` |
| remove already-indexed runs | `memware prune --containing TEXT` / `--glob GLOB` |
| remove runs already mirrored | delete them from `<dest>/transcripts` by hand; `memware backup` lists those it recognises |
| tame a recurring/dated automation prompt | `prune --turns-containing PREFIX`; add PREFIX to `ignore-markers.txt` if it heads its own sessions |
| evaluate without self-contamination | `memware-eval --corpus … --beliefs-from …` |
