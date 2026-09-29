"""`memware sync`."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from memware.cli._common import AddCommand, _hook_payload, _out
from memware.ingest import capture_disabled, sync_file, sync_tree
from memware.store import HOOK_SYNC_WAIT_MS, Store


def cmd_sync(a: argparse.Namespace) -> int:
    paths = list(a.paths)
    if a.from_hook:
        tp = _hook_payload().get("transcript_path")  # read first: that records it if need be
        if capture_disabled():
            return 0  # MEMWARE_NO_CAPTURE=1: this run must not enter the store
        if tp:
            paths.append(str(tp))
    if not paths and not a.from_hook:
        # Bare `memware sync` = catch up the configured transcript source (default
        # ~/.claude/projects). The SessionStart hook uses this to index sessions whose
        # SessionEnd never ran — e.g. a worktree manager that SIGKILLs the process group.
        from memware.config import get_dotted, load_config

        src = get_dotted(load_config(), "backup.transcript_src")
        if src:
            paths.append(str(src))
    if not paths:
        print("nothing to sync", file=sys.stderr)
        return 0
    try:
        # A hook's sync waits a few seconds for the lock, not a minute: PreCompact runs it in the
        # foreground with a 30 s timeout, and the next sync catches up from each cursor.
        with Store(a.db, busy_timeout_ms=HOOK_SYNC_WAIT_MS if a.from_hook else None) as s:
            report: dict[str, int] = {}
            for p in paths:
                path = Path(p).expanduser()
                if path.is_dir():
                    report.update(
                        sync_tree(
                            s,
                            path,
                            harness=a.harness,
                            skip_if_contains=a.skip_if_contains,
                            exclude=a.exclude,
                        )
                    )
                elif path.exists():
                    report[str(path)] = sync_file(
                        s, path, harness=a.harness, skip_if_contains=a.skip_if_contains
                    )
            _out({"added": sum(report.values()), "files": len(report)}, a.json or a.from_hook)
    except sqlite3.OperationalError as e:
        if a.from_hook and ("locked" in str(e) or "busy" in str(e)):
            return 0  # quietly: another writer held the store, and the next sync catches up
        raise
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "sync",
        "index new turns from transcripts",
        epilog=(
            "Examples:\n"
            "  memware sync                      catch up the configured transcript source\n"
            "  memware sync ~/.claude/projects   index a specific tree (idempotent)"
        ),
    )
    s.add_argument("paths", nargs="*")
    s.add_argument("--harness", default="claude-code")
    s.add_argument("--from-hook", action="store_true")
    s.add_argument(
        "--skip-if-contains",
        metavar="TEXT",
        help="skip (and un-index) files whose head contains TEXT, e.g. an eval marker",
    )
    s.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="path glob to skip when syncing a directory (repeatable)",
    )
    s.set_defaults(fn=cmd_sync)
