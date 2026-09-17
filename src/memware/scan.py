"""``memware scan``: where a value is still stored, and whether memware indexes it there.

``prune`` answers for the index. This answers the question a removal ends on: does the text still
exist anywhere memware reads it from or writes it to? It walks the transcript source on disk, not
the ``cursor`` table, so a transcript the index never held (excluded by a pattern, marked, on the
no-capture list, or not synced yet) is read as well. It checks the store file's bytes, its
write-ahead log and its search-index pages, and with ``backups`` the destination's mirrored
transcripts and snapshots.

Nothing is opened for writing, and no log is checkpointed. Files are read in binary, and SQLite files
are opened read-only (:func:`memware.residue.check_file`). A report holds paths and counts, never the
text around a match: the value is usually a secret.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from memware.ingest import (
    capture_exclude_patterns,
    default_skip_markers,
    file_contains,
    is_excluded,
    is_no_capture,
    no_capture_paths,
)
from memware.residue import FileCheck, _open_readonly, check_file, file_occurrences, value_tokens


@dataclass(frozen=True)
class TranscriptHit:
    """A transcript file holding the value."""

    path: str
    occurrences: int
    indexed: bool | None
    """Whether the store's cursor table holds this source; None for a copy in a backup."""
    excluded_by: str | None = None
    """``no-capture list``, ``capture.exclude`` or ``ignore marker``: what keeps a sync from
    indexing it, and the first of them it meets, as a sync checks. None when nothing does."""


@dataclass(frozen=True)
class Unread:
    """A file or directory scan could not read, so the report says nothing about it."""

    path: str
    reason: str


@dataclass
class ScanReport:
    store: str
    """The store path as resolved, symlinks followed: SQLite keeps the ``-wal`` beside the target."""
    store_exists: bool
    transcript_src: str
    transcripts_read: int
    transcripts: list[TranscriptHit]
    store_check: FileCheck | None
    """None when there is no store file, or it could not be read (then it is in ``unread``)."""
    store_copies: list[FileCheck]
    """``<store>.pre-restore-*.db``: the whole store as it was before a ``memware restore``."""
    indexed_gone: list[str]
    """Indexed sources whose transcript file is gone. Their turns are in the store check."""
    backup_dest: str | None = None
    """Set when the backups were scanned."""
    mirrored_read: int = 0
    mirrored: list[TranscriptHit] = field(default_factory=list)
    snapshots: list[FileCheck] = field(default_factory=list)
    unread: list[Unread] = field(default_factory=list)

    @property
    def found(self) -> bool:
        checks = [self.store_check, *self.store_copies, *self.snapshots]
        return bool(self.transcripts or self.mirrored) or any(c and c.found for c in checks)

    @property
    def complete(self) -> bool:
        """Whether every file in scope was read."""
        return not self.unread


def _walk(root: Path, unread: list[Unread]) -> list[Path]:
    """The ``*.jsonl`` files under ``root``. A directory that cannot be listed is recorded, where
    ``rglob`` would skip it without a word."""

    def failed(e: OSError) -> None:
        unread.append(Unread(str(e.filename or root), e.strerror or str(e)))

    out: list[Path] = []
    for dirpath, _dirs, names in os.walk(root, onerror=failed):
        out.extend(Path(dirpath) / n for n in names if n.endswith(".jsonl"))
    return sorted(out)


def _needles(value: str) -> list[bytes]:
    """The value as a JSONL transcript stores it: inside a JSON string, where a quote, a backslash
    and a control character are escaped, and a writer may escape non-ASCII or leave it as it is.
    Without anything to escape, that is the value verbatim. The raw form is not searched on its
    own: a value ending in a backslash would match inside its escaped form and count twice."""
    import json

    forms = [json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1]]
    return [f.encode("utf-8") for f in dict.fromkeys(forms)]


def _count(path: Path, needles: list[bytes], unread: list[Unread]) -> int | None:
    try:
        return file_occurrences(path, needles)
    except OSError as e:
        unread.append(Unread(str(path), e.strerror or str(e)))
        return None


def scan(
    value: str,
    *,
    db: str | os.PathLike[str],
    transcript_src: str | os.PathLike[str],
    backup_dest: str | os.PathLike[str] | None = None,
) -> ScanReport:
    """Where ``value`` is stored: see the module docstring. Matching is literal and
    case-sensitive in transcripts, and the store files are also counted case-folded and by their
    search terms. ``backup_dest`` adds the destination's mirrored transcripts and snapshots.

    The store's bytes, and its ``-wal``'s, are counted before anything opens it, and it is then
    opened read-only (:func:`memware.residue.check_file`), so a log a killed process left is
    counted as it is and never checkpointed into the file."""
    if not value:
        raise ValueError("an empty value would match everything")
    link = Path(db).expanduser()
    store = link.resolve()
    src = Path(transcript_src).expanduser()
    unread: list[Unread] = []
    tokens = value_tokens(value)
    needles = _needles(value)

    store_check = None
    indexed: set[str] = set()
    if store.exists():
        try:
            store_check = check_file(store, value, tokens)
        except OSError as e:
            unread.append(Unread(str(store), e.strerror or str(e)))
        else:
            try:
                con = _open_readonly(store)
                try:
                    indexed = {r[0] for r in con.execute("SELECT source FROM cursor")}
                finally:
                    con.close()
            except sqlite3.Error as e:
                unread.append(Unread(str(store), f"the cursor table could not be read: {e}"))

    if src.is_dir():
        files = {str(p.resolve()): p for p in _walk(src, unread)}
    else:
        files = {}
        unread.append(Unread(str(src), "the transcript source is not a directory"))
    gone = []
    for source in sorted(indexed):
        if source not in files:
            if Path(source).exists():
                files[source] = Path(source)  # indexed from somewhere else
            else:
                gone.append(source)

    listed, patterns, markers = (
        no_capture_paths(),
        capture_exclude_patterns(),
        default_skip_markers(),
    )
    hits, read = [], 0
    for resolved, path in files.items():
        n = _count(path, needles, unread)
        if n is None:
            continue
        read += 1
        if not n:
            continue
        excluded_by = None
        if listed and is_no_capture(resolved, listed):
            excluded_by = "no-capture list"
        elif patterns and is_excluded(resolved, patterns):
            excluded_by = "capture.exclude"
        else:
            try:
                if markers and file_contains(path, markers):
                    excluded_by = "ignore marker"
            except OSError as e:
                unread.append(Unread(str(path), e.strerror or str(e)))
        hits.append(TranscriptHit(str(path), n, resolved in indexed, excluded_by))

    copies = []
    # restore sets the old store aside beside the path it was given, which may be a link
    pattern = f"{link.stem}.pre-restore-*.db"
    for copy in sorted(
        {*link.parent.glob(pattern), *store.parent.glob(f"{store.stem}.pre-restore-*.db")}
    ):
        try:
            copies.append(check_file(copy, value, tokens))
        except OSError as e:
            unread.append(Unread(str(copy), e.strerror or str(e)))

    report = ScanReport(
        str(store), store.exists(), str(src), read, hits, store_check, copies, gone, unread=unread
    )
    if backup_dest is None:
        return report

    dest = Path(backup_dest).expanduser()
    report.backup_dest = str(dest)
    if not dest.is_dir():
        unread.append(Unread(str(dest), "the backup destination is not a directory"))
        return report
    mirror = dest / "transcripts"
    for path in _walk(mirror, unread) if mirror.is_dir() else []:
        n = _count(path, needles, unread)
        if n is None:
            continue
        report.mirrored_read += 1
        if n:
            report.mirrored.append(TranscriptHit(str(path), n, None))
    for snap in sorted(dest.glob("*.db")):  # memware-*.db snapshots, and any other store copy
        try:
            report.snapshots.append(check_file(snap, value, tokens))
        except OSError as e:
            unread.append(Unread(str(snap), e.strerror or str(e)))
    return report
