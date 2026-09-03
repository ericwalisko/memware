# Backups, restore, and the wipe trap

memware's turns are only as durable as the transcripts they came from. Claude Code deletes
session transcripts after `cleanupPeriodDays` (30 by default), so once a session ages out,
**the memware store is the only copy**. That makes two things important: keep backups, and
never wipe-and-re-backfill expecting old sessions to come back.

## The one trap to know

`rm ~/.memware/memware.db && memware backfill` looks harmless but is not: backfill re-indexes
only the transcripts *currently on disk*. Anything older than the OS retention is gone. If you
wipe, **restore from a backup — do not re-backfill.** memware helps:

- A migration that changes what memware stores takes an automatic safety snapshot first (if a
  backup destination is set), so a memware upgrade is always reversible.
- `memware backfill` warns if a backup holds materially more turns than the current store — the
  signature of a wipe — and points you to `memware restore --latest`.

## Set up backups (storage-agnostic)

A destination is just a folder. Point it at whatever you already sync or keep:

```bash
memware setup          # interactive: asks for the folder, offers transcript mirroring
# or set it directly:
memware config backup.dest "~/Dropbox/memware"        # or ~/Library/Mobile Documents/…/memware,
                                                       # ~/Google Drive/memware, /Volumes/backup/memware
memware config backup.keep_days 1,3,7,14              # tiered retention (default)
```

Then snapshot:

```bash
memware backup         # writes memware-YYYYMMDD-HHMMSS.db, prunes to the retention tiers,
                       # and (by default) mirrors transcripts into <dest>/transcripts
```

**Retention** keeps the newest snapshot plus the newest one at least 1, 3, 7 and 14 days old —
roughly a 1-, 3-, 7- and 14-day-old backup at all times, not an unbounded pile. Tune with
`backup.keep_days`.

**Transcripts.** `backup.include_transcripts` (on by default) mirrors your transcript source
(default `~/.claude/projects`) into `<dest>/transcripts`, additively — an append-only archive
that outlives the 30-day cleanup. Already back transcripts up elsewhere? Point memware at that
location with `backup.transcript_src` and it will read from there too.

## Schedule it

Any scheduler works; memware just needs to run `memware backup` daily. macOS `launchd`, Linux
`cron`, or a coding-agent cron all do. Example (cron, 3 a.m.):

```cron
0 3 * * * /path/to/memware backup >/dev/null 2>&1
```

## Restore

```bash
memware restore --latest              # newest snapshot in backup.dest
memware restore --from ~/Dropbox/memware/memware-20260901-030000.db
```

Restore copies the current store aside first (`memware.pre-restore-*.db`), so a restore is
itself reversible.

## Delete everything (guarded)

`memware nuke` permanently deletes the store, its config, review files, **and every snapshot
in the backup destination**. It cannot happen by accident — you must type the exact phrase:

```bash
memware nuke
# This permanently deletes: store, config, N snapshots…
# Type exactly:  DELETE ALL MEMWARE DATA
```
