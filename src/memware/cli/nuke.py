"""`memware nuke`."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from memware import relevance
from memware.cli._common import AddCommand, _out

NUKE_PHRASE = "DELETE ALL MEMWARE DATA"


def cmd_nuke(a: argparse.Namespace) -> int:
    """Permanently delete the store, its config, review files, labeled prompts (``labels/``) AND
    every snapshot in the backup destination. Guarded by a typed confirmation so it cannot happen
    by accident."""
    from memware import backup as bk
    from memware.config import config_path, get_dotted, load_config, memware_home

    dest = get_dotted(load_config(), "backup.dest")
    snaps = bk.list_snapshots(dest) if dest else []
    targets = [Path(a.db).expanduser(), Path(str(a.db) + "-wal"), Path(str(a.db) + "-shm")]
    home = memware_home()
    for name in (
        "ignore-markers.txt",
        "no-capture.txt",
        "no-capture.txt.lock",
        "review-outbox.jsonl",
        "review-inbox.jsonl",
        relevance.LOG_NAME,  # holds the text of prompts, when the relevance filter was on
        relevance.USAGE_NAME,
        relevance.TRIM_STAMP,
        relevance.LOG_NAME + ".tmp",  # a trim that was killed mid-write
    ):
        targets.append(home / name)
    targets.append(config_path())
    labels = home / "labels"  # prompts labeled relevant or not, to calibrate injection
    print("This permanently deletes:")
    print(f"  store:      {a.db} (+ wal/shm)")
    print(f"  config:     {config_path()}")
    if labels.exists() or labels.is_symlink():
        print(f"  labels:     {labels}/ (labeled prompt text)")
    print(
        f"  snapshots:  {len(snaps)} in {dest}"
        if dest
        else "  snapshots:  (no backup dest configured)"
    )
    print(f"\nType exactly:  {NUKE_PHRASE}")
    typed = a.confirm
    if typed is None:
        try:
            typed = input("> ").strip()
        except EOFError:
            typed = ""
    if typed != NUKE_PHRASE:
        print("phrase did not match — nothing deleted", file=sys.stderr)
        return 1
    removed = 0
    for snap in snaps:
        snap.unlink(missing_ok=True)
        removed += 1
    for t in targets:
        if t.exists():
            t.unlink()
            removed += 1
    if labels.is_dir() and not labels.is_symlink():
        removed += sum(1 for p in labels.rglob("*") if not p.is_dir() or p.is_symlink())
        shutil.rmtree(labels)
    elif labels.exists() or labels.is_symlink():  # a link is removed, never followed
        labels.unlink()
        removed += 1
    _out({"deleted_files": removed, "snapshots_deleted": len(snaps)}, a.json)
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "nuke",
        "permanently delete the store, config, and ALL backups (typed confirmation required)",
    )
    s.add_argument(
        "--confirm",
        metavar="PHRASE",
        help='must equal "DELETE ALL MEMWARE DATA" (else you are prompted)',
    )
    s.set_defaults(fn=cmd_nuke)
