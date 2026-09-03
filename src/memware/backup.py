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


def newest_age_hours(dest_dir: str | os.PathLike[str]) -> float | None:
    """Hours since the newest snapshot in ``dest_dir``, or None if there are none."""
    snaps = list_snapshots(dest_dir)
    if not snaps:
        return None
    return (_now().timestamp() - _stamp(snaps[0]).timestamp()) / 3600.0


def _age_days(p: Path, now: datetime) -> float:
    return (now.timestamp() - _stamp(p).timestamp()) / 86400.0


def apply_retention(dest_dir: str | os.PathLike[str], keep_days: list[int]) -> list[Path]:
    """Promotion retention: since we only ever *create* fresh snapshots, a snapshot must be
    allowed to **age forward** into the next tier rather than be pruned between tiers.

    Bands are the contiguous intervals ``(0, d0], (d0, d1], …`` for sorted ``keep_days``
    (default 1,3,7,14). We keep the newest snapshot overall (tomorrow's ~1-day-old) and the
    *oldest* snapshot in each band — the oldest is the one on the leading edge, so as days
    pass it crosses into the next band and stays kept, i.e. the same file serves as the ~1-,
    then ~3-, ~7-, ~14-day-old. Everything else, and anything older than the largest tier,
    is pruned. Result: always a ~1-day-old, roughly one per tier (they drift; that's fine),
    and a bounded pile of ~len(keep_days)+1. Returns the deleted paths."""
    snaps = list_snapshots(dest_dir)  # newest first
    if not snaps:
        return []
    now = _now()
    days = sorted(set(int(d) for d in keep_days))
    keep: set[Path] = {snaps[0]}  # always the freshest
    edges = [0.0, *[float(d) for d in days]]
    for lo, hi in zip(edges, edges[1:], strict=False):
        band = [p for p in snaps if lo < _age_days(p, now) <= hi]
        if band:
            keep.add(band[-1])  # oldest in the band (snaps are newest-first) -> promotes forward
    deleted = []
    for p in snaps:
        if p not in keep:  # includes anything older than the largest tier
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
