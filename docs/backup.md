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

## How it stays current on its own (no scheduler)

Once a backup destination is set (`memware setup`), the Claude Code plugin and the Hermes
provider back up **automatically at session end, at most once per `backup.auto_interval_hours`
(default 20)**. This rides your usage rather than a clock, so it is immune to the sleep problem:
a laptop only needs to be awake *while you are using it*, which it is. Nothing to schedule, and
a missed day simply catches up on your next session. Turn it off with
`memware config backup.auto false`.

Both the snapshot and the transcript mirror happen in that one throttled run, so a laptop user
who never sets up a cron still gets: the store snapshotted off-machine, and transcripts archived
before the OS's 30-day cleanup can reach them.

## Scheduling on an always-on machine (optional)

If a box is always on and you want backups on a fixed clock regardless of whether anyone used
it that day, add a scheduled job. Pick the one that fits — and note that **plain `cron` skips
runs that fall while the machine is asleep or off**, so it is the wrong choice for a laptop:

- **macOS — `launchd`** (sleep-tolerant: a missed `StartCalendarInterval` runs on wake):
  a `~/Library/LaunchAgents/*.plist` with `StartCalendarInterval` (hour 3) calling
  `memware backup`.
- **Linux — `systemd` timer** with `Persistent=true` (runs a missed timer on next boot/wake):
  a `memware-backup.timer` + `.service` pair, `OnCalendar=daily`.
- **Linux — `anacron`** if the machine is often off: a daily job that runs when the box is next
  up. Plain `cron` does not do this.
- **Coding-agent cron** (e.g. a Hermes `--no-agent` job): fine on an always-on host; same sleep
  caveat as `cron`.

`memware backup --if-stale HOURS` is safe to call from any of these too — it no-ops if a recent
snapshot already exists, so the automatic session-end backup and a scheduled one never
double up.

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
