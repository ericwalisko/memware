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
from itertools import pairwise
from pathlib import Path

from memware.ingest import default_skip_markers, file_contains, is_no_capture, no_capture_paths

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
    for lo, hi in pairwise(edges):
        band = [p for p in snaps if lo < _age_days(p, now) <= hi]
        if band:
            keep.add(band[-1])  # oldest in the band (snaps are newest-first) -> promotes forward
    deleted = []
    for p in snaps:
        if p not in keep:  # includes anything older than the largest tier
            p.unlink(missing_ok=True)
            deleted.append(p)
    return deleted


class MirrorResult:
    """What ``mirror_transcripts`` did: files copied, files it had to leave for next time, and
    files it must never copy.

    ``skipped`` is a list of ``(target, reason)`` — the run does not abort on one bad file.
    ``excluded_no_capture`` and ``excluded_marker`` are the source transcripts left out because
    they are on the no-capture list or carry a skip marker. ``left_in_backup`` holds the copies
    of those that an earlier run already made; the mirror reports them and never deletes them."""

    __slots__ = ("copied", "excluded_marker", "excluded_no_capture", "left_in_backup", "skipped")

    def __init__(self) -> None:
        self.copied = 0
        self.skipped: list[tuple[Path, str]] = []
        self.excluded_no_capture: list[Path] = []
        self.excluded_marker: list[Path] = []
        self.left_in_backup: list[Path] = []

    def __int__(self) -> int:  # the pre-0.3.1 return type was the copied count
        return self.copied


def mirror_transcripts(
    src_root: str | os.PathLike[str], dest_dir: str | os.PathLike[str]
) -> MirrorResult:
    """Copy new/changed ``*.jsonl`` transcripts from ``src_root`` into ``dest_dir`` (additive,
    never deletes — an append-only archive that outlives the OS's own transcript cleanup).

    Two rules, both learned from a synced destination (Dropbox, 2026-09-04 → 09-08, five
    nightly runs in a row died here):

    1. The target is never opened for writing. A synced folder evicts files it has already
       uploaded to "dataless" placeholders, and opening one of those for write makes the
       sync engine materialise it first, which fails with ``EDEADLK`` (errno 11 on macOS) roughly
       half the time. So the copy goes to a temp file beside the target and is renamed
       over it — ``os.replace`` swaps the directory entry and never touches the old bytes.
    2. One file that cannot be written is skipped and reported, not raised. A mirror is
       best-effort and retried on every run; the snapshot that ran before it is the thing
       that must not be lost, and an exception here used to take the whole ``backup`` exit
       code (and the cron that reads it) down with it.

    A transcript that sync would never index is never copied either: one on the no-capture
    list or in a listed session's subagent directory, or one whose head carries a skip marker. The destination is often a synced folder,
    which is further from the machine than the store is.
    """
    src = Path(src_root).expanduser()
    dest = (Path(dest_dir).expanduser()) / "transcripts"
    result = MirrorResult()
    if not src.exists():
        return result
    listed = no_capture_paths()
    markers = default_skip_markers()
    for f in src.rglob("*.jsonl"):
        rel = f.relative_to(src)
        target = dest / rel
        try:
            if listed and is_no_capture(f.resolve(), listed):
                excluded: list[Path] | None = result.excluded_no_capture
            elif markers and file_contains(f, markers):
                excluded = result.excluded_marker
            else:
                excluded = None
            if excluded is not None:
                excluded.append(f)
                if target.exists():
                    result.left_in_backup.append(target)
                continue
            if (
                target.exists()
                and target.stat().st_mtime >= f.stat().st_mtime
                and target.stat().st_size == f.stat().st_size
            ):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.mw-tmp")
            try:
                shutil.copy2(f, tmp)
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
            result.copied += 1
        except OSError as e:
            result.skipped.append((target, f"{e.strerror or e} (errno {e.errno})"))
    return result


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
