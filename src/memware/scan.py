"""``memware scan``: where a value is still stored, and whether memware indexes it there.

``prune`` answers for the index. This answers the question a removal ends on: does the text still
exist anywhere memware reads it from or writes it to? It walks the transcript source on disk, not
the ``cursor`` table, so a transcript the index never held (excluded by a pattern, marked, on the
no-capture list, or not synced yet) is read as well. It checks the store file's bytes, its
write-ahead log and its search-index pages, and with ``backups`` the destination's mirrored
transcripts and snapshots.

Nothing is opened for writing. Files are read in binary, the store through a ``query_only``
handle, and snapshots and copies ``immutable``, so not even a side file appears beside them. A
report holds paths and counts, never the text around a match: the value is usually a secret.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from memware.derive import open_readonly
from memware.ingest import (
    beliefs_holding,
    capture_exclude_patterns,
    default_skip_markers,
    file_contains,
    file_occurrences,
    is_excluded,
    is_no_capture,
    no_capture_paths,
)

FTS_TABLES = ("passage_fts", "belief_fts")
TOKENIZER = "porter unicode61"
"""The tokenizer both FTS tables in :data:`memware.store.SCHEMA` declare."""

_LEAF_ROWID_MIN = 1 << 37
"""FTS5 ``%_data`` rowids are ``segid << 37 | dlidx << 36 | height << 31 | page``; a segment id
starts at 1, so every segment page is at least this, and rowids 1 and 10 (averages, structure)
are not."""


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


@dataclass(frozen=True)
class DbCheck:
    """One SQLite file: the store, a copy restore set aside, or a snapshot."""

    path: str
    occurrences: int
    """The value's UTF-8 bytes in the file, as given."""
    occurrences_any_case: int
    """The same with ASCII case folded, which is how FTS5 stores a term."""
    wal_occurrences: int | None = None
    """In the ``-wal`` file beside it, as given and case-folded; None when there is none."""
    wal_occurrences_any_case: int | None = None
    turns: int | None = None
    """Turns whose text holds the value; None when the file could not be queried."""
    beliefs: int | None = None
    """Beliefs, in any status, whose subject, relation or value holds it."""
    tokens: int = 0
    """How many search terms the value makes, as the store's tokenizer splits it."""
    index_tokens: dict[str, int] = field(default_factory=dict)
    """FTS table -> how many of those terms its index pages hold, deleted entries included."""
    free_pages: int | None = None
    """Pages the file holds and no table uses. Not searched as index pages; VACUUM drops them."""
    error: str | None = None
    """Why the file could not be queried; the byte counts above still stand."""

    @property
    def found(self) -> bool:
        counted = (
            self.occurrences,
            self.occurrences_any_case,
            self.wal_occurrences,
            self.wal_occurrences_any_case,
            self.turns,
            self.beliefs,
        )
        whole = bool(self.tokens) and self.tokens in self.index_tokens.values()
        return whole or any(counted)


@dataclass
class ScanReport:
    store: str
    transcript_src: str
    transcripts_read: int
    transcripts: list[TranscriptHit]
    store_check: DbCheck | None
    """None when there is no store file."""
    store_copies: list[DbCheck]
    """``<store>.pre-restore-*.db``: the whole store as it was before a ``memware restore``."""
    indexed_gone: list[str]
    """Indexed sources whose transcript file is gone. Their turns are in the store check."""
    backup_dest: str | None = None
    """Set when the backups were scanned."""
    mirrored_read: int = 0
    mirrored: list[TranscriptHit] = field(default_factory=list)
    snapshots: list[DbCheck] = field(default_factory=list)
    unread: list[Unread] = field(default_factory=list)

    @property
    def found(self) -> bool:
        checks = [self.store_check, *self.store_copies, *self.snapshots]
        return bool(self.transcripts or self.mirrored) or any(c and c.found for c in checks)

    @property
    def complete(self) -> bool:
        """Whether every file in scope was read."""
        return not self.unread


def value_tokens(value: str) -> list[str]:
    """The terms FTS5 indexes ``value`` under: tokenized, case-folded and stemmed by the store's own
    tokenizer, in an in-memory table."""
    con = sqlite3.connect(":memory:")
    try:
        con.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='{TOKENIZER}')")
        con.execute("INSERT INTO t(x) VALUES (?)", (value,))
        con.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, row)")
        return [str(r[0]) for r in con.execute("SELECT term FROM v ORDER BY term")]
    finally:
        con.close()


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    """An SQLite varint at ``buf[i]``: the value and the offset after it."""
    v = 0
    for _ in range(8):
        byte = buf[i]
        i += 1
        v = (v << 7) | (byte & 0x7F)
        if byte < 0x80:
            return v, i
    return (v << 8) | buf[i], i + 1


def leaf_terms(page: bytes) -> Iterator[bytes]:
    """Every term on one FTS5 leaf page, with its prefix compression undone.

    A leaf starts with two big-endian 16-bit offsets, the second where its footer begins. The
    footer holds one varint per term on the page, the byte offset of the first term and then the
    distance to each next one. The first term is stored whole, as a length and bytes; each later
    one as the bytes it shares with the term before, the bytes it adds, and those bytes. A term
    carries one leading byte, ``0`` for the main index.

    Reading pages rather than ``fts5vocab`` is the point. A deleted row leaves its terms on a page
    until a merge, and fts5vocab skips them, so it says the value is gone while the file holds it.
    A page too short or malformed raises IndexError."""
    if len(page) < 4 or int.from_bytes(page[2:4], "big") >= len(page):
        return  # no footer: a page that only continues a doclist
    footer = int.from_bytes(page[2:4], "big")
    at, i = _varint(page, footer)
    term = b""
    while True:
        if not term:
            n, j = _varint(page, at)
            term = page[j : j + n]
        else:
            shared, j = _varint(page, at)
            n, j = _varint(page, j)
            term = term[:shared] + page[j : j + n]
        yield term
        if i >= len(page):
            return
        step, i = _varint(page, i)
        at += step


def index_holds(con: sqlite3.Connection, table: str, terms: list[str]) -> int:
    """How many of ``terms`` appear on a leaf page of the FTS5 ``table``, deleted entries
    included. Stops reading once every term is found."""
    wanted = {b"0" + t.encode("utf-8") for t in terms}
    held: set[bytes] = set()
    rows = con.execute(f"SELECT id, block FROM {table}_data WHERE id >= ?", (_LEAF_ROWID_MIN,))
    for rowid, page in rows:
        if (rowid >> 31) & 0x3F:  # a doclist-index page, not a leaf
            continue
        held.update(t for t in leaf_terms(bytes(page)) if t in wanted)
        if held == wanted:
            break
    return len(held)


def _wal_counts(path: Path, needle: bytes) -> tuple[int | None, int | None]:
    wal = Path(f"{path}-wal")
    if not wal.exists():
        return None, None
    return file_occurrences(wal, [needle]), file_occurrences(wal, [needle], any_case=True)


def check_db(path: Path, value: str, tokens: list[str], *, live: bool) -> DbCheck:
    """Count the value in one SQLite file's bytes and then query it. ``live`` is the store, read
    through a ``query_only`` handle and with its ``-wal`` counted; anything else is opened
    ``immutable``. The bytes are counted before the file is opened, so the open cannot move pages
    between the log and the file first. An OSError reading the bytes propagates."""
    needle = value.encode("utf-8")
    found = file_occurrences(path, [needle])
    any_case = file_occurrences(path, [needle], any_case=True)
    wal, wal_any_case = _wal_counts(path, needle)
    check = DbCheck(str(path), found, any_case, wal, wal_any_case, tokens=len(tokens))
    try:
        con = (
            open_readonly(path)
            if live
            else sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        )
        try:
            q = con.execute
            return replace(
                check,
                turns=int(
                    q("SELECT count(*) FROM turn WHERE instr(text, ?) > 0", (value,)).fetchone()[0]
                ),
                beliefs=beliefs_holding(con, value),
                free_pages=int(q("PRAGMA freelist_count").fetchone()[0]),
                index_tokens={t: index_holds(con, t, tokens) for t in FTS_TABLES},
            )
        finally:
            con.close()
    except (sqlite3.Error, IndexError) as e:
        return replace(check, error=f"{type(e).__name__}: {e}")


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
    """The value as a transcript stores it: verbatim, or escaped inside a JSON string, where a
    quote, a backslash or a control character is escaped, and some writers escape non-ASCII."""
    import json

    forms = [value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1]]
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
    search terms. ``backup_dest`` adds the destination's mirrored transcripts and snapshots."""
    if not value:
        raise ValueError("an empty value would match everything")
    store = Path(db).expanduser()
    src = Path(transcript_src).expanduser()
    unread: list[Unread] = []
    tokens = value_tokens(value)
    needles = _needles(value)

    indexed: set[str] = set()
    if store.exists():
        try:
            con = open_readonly(store)
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

    store_check = None
    if store.exists():
        try:
            store_check = check_db(store, value, tokens, live=True)
        except OSError as e:
            unread.append(Unread(str(store), e.strerror or str(e)))
    copies = []
    for copy in sorted(store.parent.glob(f"{store.stem}.pre-restore-*.db")):
        try:
            copies.append(check_db(copy, value, tokens, live=False))
        except OSError as e:
            unread.append(Unread(str(copy), e.strerror or str(e)))

    report = ScanReport(str(store), str(src), read, hits, store_check, copies, gone, unread=unread)
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
            report.snapshots.append(check_db(snap, value, tokens, live=False))
        except OSError as e:
            unread.append(Unread(str(snap), e.strerror or str(e)))
    return report
