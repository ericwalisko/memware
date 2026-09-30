"""`memware backup`."""

from __future__ import annotations

import argparse
import sys

from memware.cli._common import AddCommand, _hiding_verdict, _out


def cmd_backup(a: argparse.Namespace) -> int:
    from memware import backup as bk
    from memware.config import get_dotted, load_config

    cfg = load_config()
    dest = a.dest or get_dotted(cfg, "backup.dest")
    if not dest:
        if a.if_stale is not None:
            return 0  # hook-driven: no destination configured yet, stay silent
        print(
            "no backup destination — pass --dest DIR or run `memware setup` "
            "(point it at a Dropbox/iCloud/Drive folder or an external disk)",
            file=sys.stderr,
        )
        return 2
    if a.if_stale is not None:
        # throttle: skip if a snapshot already exists within the window (so this is safe to
        # call from every session-end hook — the backup rides usage, not a clock).
        if not get_dotted(cfg, "backup.auto") and not a.dest:
            return 0
        age = bk.newest_age_hours(dest)
        if age is not None and age < a.if_stale:
            if not a.quiet:
                _out({"skipped": "recent", "age_hours": round(age, 1)}, a.json)
            return 0
    keep = a.keep or get_dotted(cfg, "backup.keep_days") or [1, 3, 7, 14]
    out = bk.snapshot(a.db, dest)
    deleted = bk.apply_retention(dest, keep)
    result: dict[str, object] = {
        "snapshot": str(out),
        "kept": [p.name for p in bk.list_snapshots(dest)],
        "pruned": [p.name for p in deleted],
    }
    include = a.transcripts or (
        a.transcripts is None and bool(get_dotted(cfg, "backup.include_transcripts"))
    )
    if include:
        src = a.transcript_src or get_dotted(cfg, "backup.transcript_src") or "~/.claude/projects"
        mirrored = bk.mirror_transcripts(src, dest)
        result["transcripts_mirrored"] = mirrored.copied
        result["transcripts_skipped_no_capture"] = len(mirrored.excluded_no_capture)
        result["transcripts_skipped_glob"] = len(mirrored.excluded_glob)
        result["transcripts_skipped_marker"] = len(mirrored.excluded_marker)
        hiding = _hiding_verdict(len(mirrored.excluded_glob), mirrored.seen)
        if hiding:
            print(hiding, file=sys.stderr)
        if mirrored.left_in_backup:
            # Copies an earlier run made before the transcript was listed, excluded or marked. memware
            # never deletes from a destination, so a person has to; say where, every run.
            result["transcripts_left_in_backup"] = [str(p) for p in mirrored.left_in_backup]
            for target in mirrored.left_in_backup[:5]:
                print(
                    f"excluded transcript already in the backup, remove it by hand: {target}",
                    file=sys.stderr,
                )
            if len(mirrored.left_in_backup) > 5:
                print(f"... and {len(mirrored.left_in_backup) - 5} more", file=sys.stderr)
        if mirrored.skipped:
            # Reported, not fatal: the snapshot above already succeeded and the mirror is
            # retried on every run. Silence would hide a destination that never takes
            # writes; a traceback took five nightly crons down over one dataless file.
            result["transcripts_skipped"] = len(mirrored.skipped)
            for target, why in mirrored.skipped[:5]:
                print(f"transcript not mirrored: {target}: {why}", file=sys.stderr)
            if len(mirrored.skipped) > 5:
                print(f"... and {len(mirrored.skipped) - 5} more", file=sys.stderr)
    if not a.quiet:
        _out(result, a.json)
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "backup",
        "snapshot the store to a folder (Dropbox/iCloud/Drive/disk) with tiered retention",
        epilog=(
            "Examples:\n"
            "  memware backup             snapshot + transcript mirror to backup.dest\n"
            "  memware restore --latest   after a wipe (never re-backfill)"
        ),
    )
    s.add_argument("--dest", metavar="DIR", help="destination (default: backup.dest from config)")
    s.add_argument(
        "--keep",
        type=lambda v: [int(x) for x in v.replace(",", " ").split()],
        metavar="D1,D3,D7…",
        help="age buckets to keep (default 1,3,7,14)",
    )
    s.add_argument(
        "--transcripts",
        action="store_true",
        default=None,
        help="also mirror transcripts into <dest>/transcripts",
    )
    s.add_argument("--no-transcripts", dest="transcripts", action="store_false")
    s.add_argument("--transcript-src", metavar="DIR")
    s.add_argument(
        "--if-stale",
        type=float,
        metavar="HOURS",
        help="only back up if the newest snapshot is older than HOURS "
        "(safe to call from every session-end hook; no-op when no dest is set)",
    )
    s.add_argument("--quiet", action="store_true", help="print nothing on success")
    s.set_defaults(fn=cmd_backup)
