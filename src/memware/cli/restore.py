"""`memware restore`."""

from __future__ import annotations

import argparse
import sys

from memware.cli._common import AddCommand, _out
from memware.store import Store


def _resolve_backup_dest(a: argparse.Namespace) -> str | None:
    from memware.config import get_dotted, load_config

    dest: str | None = a.dest or get_dotted(load_config(), "backup.dest")
    return dest


def cmd_restore(a: argparse.Namespace) -> int:
    from memware import backup as bk

    dest = _resolve_backup_dest(a)
    snap = a.from_file
    if not snap:
        if not dest:
            print("no backup destination configured; pass --from FILE", file=sys.stderr)
            return 2
        snaps = bk.list_snapshots(dest)
        if not snaps:
            print(f"no snapshots in {dest}", file=sys.stderr)
            return 2
        snap = str(snaps[0])
    prev = bk.restore(snap, a.db)
    with Store(a.db) as s:
        stats = s.stats()
    _out({"restored_from": snap, "previous_store_saved_to": str(prev), **stats}, a.json)
    return 0


def register(add: AddCommand) -> None:
    s = add("restore", "replace the store with a snapshot (the current store is saved aside first)")
    s.add_argument(
        "--from",
        dest="from_file",
        metavar="FILE",
        help="snapshot file (default: latest in backup.dest)",
    )
    s.add_argument("--dest", metavar="DIR", help="backup destination to pick the latest from")
    s.set_defaults(fn=cmd_restore)
