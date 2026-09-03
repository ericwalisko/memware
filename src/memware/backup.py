"""Backups: consistent SQLite snapshots with tiered retention, restore, transcript mirror.

Storage-agnostic — a "destination" is just a directory. Point it at a synced folder
(Dropbox, iCloud Drive, Google Drive), an external disk, or a network mount; memware only
writes files there. Snapshots use ``VACUUM INTO`` so a single self-contained file is
captured atomically even while the store is in WAL mode and being written.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SNAPSHOT_GLOB = "memware-*.db"
_SNAPSHOT_RE = re.compile(r"memware-(\d{8}-\d{6})\.db$")


def _now() -> datetime:
    return datetime.now(UTC)


def snapshot(store_path: str | os.PathLike[str], dest_dir: str | os.PathLike[str]) -> Path:
    """Write a consistent snapshot of the store into ``dest_dir``; return its path."""
    src = Path(store_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"no store at {src}")
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"memware-{_now().strftime('%Y%m%d-%H%M%S')}.db"
    con = sqlite3.connect(str(src))
    try:
        con.execute("VACUUM INTO ?", (str(out),))  # atomic, self-contained, WAL-safe
    finally:
        con.close()
    return out


def list_snapshots(dest_dir: str | os.PathLike[str]) -> list[Path]:
    """Snapshots in ``dest_dir``, newest first (by the timestamp in the filename)."""
    dest = Path(dest_dir).expanduser()
    if not dest.exists():
        return []
    snaps = [p for p in dest.glob(SNAPSHOT_GLOB) if _SNAPSHOT_RE.search(p.name)]
    return sorted(snaps, key=lambda p: p.name, reverse=True)


def _stamp(p: Path) -> datetime:
    m = _SNAPSHOT_RE.search(p.name)
    assert m
    return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)


def apply_retention(dest_dir: str | os.PathLike[str], keep_days: list[int]) -> list[Path]:
    """Tiered retention. Always keep the newest snapshot; then for each age bucket in
    ``keep_days`` (e.g. [1,3,7,14]) keep the newest snapshot at least that old. Delete the
    rest. Returns the deleted paths. This yields, over time, roughly one 1-, 3-, 7- and
    14-day-old backup plus the latest — not an unbounded pile."""
    snaps = list_snapshots(dest_dir)
    if not snaps:
        return []
    now = _now()
    keep: set[Path] = {snaps[0]}  # newest always
    for days in sorted(set(keep_days)):
        cutoff = now.timestamp() - days * 86400
        aged = [p for p in snaps if _stamp(p).timestamp() <= cutoff]
        if aged:
            keep.add(aged[0])  # newest that is at least `days` old
    deleted = []
    for p in snaps:
        if p not in keep:
            p.unlink(missing_ok=True)
            deleted.append(p)
    return deleted


def mirror_transcripts(src_root: str | os.PathLike[str], dest_dir: str | os.PathLike[str]) -> int:
    """Copy new/changed ``*.jsonl`` transcripts from ``src_root`` into ``dest_dir`` (additive,
    never deletes — an append-only archive that outlives the OS's own transcript cleanup).
    Returns the number of files copied."""
    src = Path(src_root).expanduser()
    dest = (Path(dest_dir).expanduser()) / "transcripts"
    if not src.exists():
        return 0
    copied = 0
    for f in src.rglob("*.jsonl"):
        rel = f.relative_to(src)
        target = dest / rel
        if (
            target.exists()
            and target.stat().st_mtime >= f.stat().st_mtime
            and target.stat().st_size == f.stat().st_size
        ):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        copied += 1
    return copied


def restore(snapshot_path: str | os.PathLike[str], store_path: str | os.PathLike[str]) -> Path:
    """Replace the store with a snapshot, after safety-copying the current store aside.
    Returns the path of the safety copy of the previous store (or a note if none existed)."""
    snap = Path(snapshot_path).expanduser()
    if not snap.exists():
        raise FileNotFoundError(f"no snapshot at {snap}")
    store = Path(store_path).expanduser()
    store.parent.mkdir(parents=True, exist_ok=True)
    backup_of_current = store.with_suffix(f".pre-restore-{_now().strftime('%Y%m%d-%H%M%S')}.db")
    if store.exists():
        shutil.copy2(store, backup_of_current)
    for suffix in ("-wal", "-shm"):
        Path(str(store) + suffix).unlink(missing_ok=True)  # drop stale WAL of the old store
    shutil.copy2(snap, store)
    return backup_of_current
