"""`memware backfill`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from memware.cli._common import AddCommand, _out
from memware.cli.notice import _maybe_setup_hint
from memware.ingest import sync_tree
from memware.store import Store


def _warn_if_backup_is_larger(s: Store, a: argparse.Namespace) -> None:
    """If a backup holds materially more than this store, the user may have wiped it while
    transcripts past the OS's 30-day cleanup are already gone. Backfill can only re-index
    what is on disk, so steer them to restore instead. Warning only — backfill never deletes."""
    try:
        from memware import backup as bk
        from memware.config import get_dotted, load_config

        dest = get_dotted(load_config(), "backup.dest")
        snaps = bk.list_snapshots(dest) if dest else []
        if not snaps:
            return
        here = s.stats()["turns"]
        import sqlite3

        con = sqlite3.connect(f"file:{snaps[0]}?mode=ro", uri=True)
        try:
            there = int(con.execute("SELECT count(*) FROM turn").fetchone()[0])
        finally:
            con.close()
        if there > here + 100:
            print(
                f"WARNING: backup {snaps[0].name} holds {there:,} turns; this store has {here:,}. "
                f"If you wiped the store and transcripts older than your OS's retention are gone, "
                f"backfill cannot bring them back — restore instead:\n"
                f"    memware restore --latest\n"
                f"Continuing will index only the transcripts currently on disk.",
                file=sys.stderr,
            )
    except Exception:
        pass


def cmd_backfill(a: argparse.Namespace) -> int:
    """One-time index of existing transcripts on a fresh machine.

    The plugin only captures new sessions; this reads what is already on disk.
    Idempotent — safe to re-run — and it honours the ignore-markers list.
    """
    _maybe_setup_hint(a)
    root = Path(a.root).expanduser()
    if not root.exists():
        print(f"nothing to backfill: {root} does not exist", file=sys.stderr)
        return 0
    with Store(a.db) as s:
        _warn_if_backup_is_larger(s, a)
        report = sync_tree(s, root, harness=a.harness, exclude=a.exclude)
        added = sum(report.values())
        stats = s.stats()
    _out(
        {
            "root": str(root),
            "files": len(report),
            "turns_added": added,
            "turns_total": stats["turns"],
            "sessions": stats["sessions"],
        },
        a.json,
    )
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "backfill",
        "one-time index of existing transcripts (run once on a new machine)",
        epilog="Example:\n  memware backfill        index ~/.claude/projects once on a new machine",
    )
    s.add_argument(
        "root",
        nargs="?",
        default="~/.claude/projects",
        help="transcript root (default: ~/.claude/projects)",
    )
    s.add_argument("--harness", default="claude-code")
    s.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    s.set_defaults(fn=cmd_backfill)
