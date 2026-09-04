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
The Claude Code hooks, the Hermes provider, and `memware sync --from-hook` all
honour it and do nothing.

```bash
MEMWARE_NO_CAPTURE=1 claude -p "…"          # this session is never captured
MEMWARE_NO_CAPTURE=1 my-eval-harness.sh      # neither is anything it launches
```

It is the cheapest and most complete option **for runs you control going
forward**. It does nothing for transcripts already on disk, and nothing if a run
forgets to set it.

## Layer 2 — the content filter (catches everything, retroactively too)

Put a stable marker string in every prompt your evaluation sends
(memware uses `[memware-eval]`), then list markers in
`~/.memware/ignore-markers.txt` — one per line, `#` comments allowed:

```text
# any transcript whose head contains one of these is never indexed by any sync
[memware-eval]
Answer briefly using only what you know     # an older harness's prompt, no marker of its own
```

Every `sync` — the hooks, the provider, `memware sync`, a full-tree backfill,
the nightly derive lane — checks this list (and the `MEMWARE_IGNORE_MARKERS`
env var, same format) and skips any file whose head contains a listed string.
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
   (or your own marker) in every prompt — belt and braces.
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
| never capture this run | `MEMWARE_NO_CAPTURE=1` in its environment |
| never capture anything matching a phrase | add the phrase to `~/.memware/ignore-markers.txt` |
| remove already-indexed runs | `memware prune --containing TEXT` / `--glob GLOB` |
| tame a recurring/dated automation prompt | `prune --turns-containing PREFIX`; add PREFIX to `ignore-markers.txt` if it heads its own sessions |
| evaluate without self-contamination | `memware-eval --corpus … --beliefs-from …` |
