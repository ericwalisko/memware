"""What a store file still holds of a value: bytes, rows, and search-index terms.

Shared by ``memware prune``, which checks the file before it decides to scrub and again after, and
``memware scan``. Nothing here writes to a store: the checks read files in binary and query through
the connection they are given, or a read-only one they open themselves.

A deleted row leaves its words on FTS5 index pages until the index merges, lowercased and stored
as only the bytes that differ from the term before. ``fts5vocab`` skips them, and a byte search
cannot match them, so :func:`leaf_terms` reads the pages themselves.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

FTS_TABLES = ("passage_fts", "belief_fts")
TOKENIZER = "porter unicode61"
"""The tokenizer both FTS tables in :data:`memware.store.SCHEMA` declare."""

_LEAF_ROWID_MIN = 1 << 37
"""FTS5 ``%_data`` rowids are ``segid << 37 | dlidx << 36 | height << 31 | page``; a segment id
starts at 1, so every segment page is at least this, and rowids 1 and 10 (averages, structure)
are not."""

_FTS_SHADOW = ("_data", "_idx", "_content", "_docsize", "_config")


def file_windows(path: Path, overlap: int, *, chunk_bytes: int = 1 << 20) -> Iterator[bytes]:
    """The file read whole, a chunk at a time. Each window is a chunk led by the ``overlap`` bytes
    before it, so a needle of up to ``overlap + 1`` bytes that spans a chunk boundary still sits
    whole inside one window."""
    carry = b""
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_bytes):
            window = carry + chunk
            yield window
            carry = window[-overlap:] if overlap else b""


def file_occurrences(
    path: Path, needles: list[bytes], *, any_case: bool = False, chunk_bytes: int = 1 << 20
) -> int:
    """At how many offsets any of ``needles`` starts in the file, read whole. Overlapping
    occurrences each count, and needles that start at the same offset count once. With
    ``any_case``, ASCII letters match in either case, as SQLite folds them.

    A match is found in the window where it ends, and its absolute offset is remembered until no
    later window can reach it, so the count is the same however the chunks fall."""
    wanted = [n.lower() if any_case else n for n in dict.fromkeys(needles) if n]
    if not wanted:
        return 0
    overlap = max(len(n) for n in wanted) - 1
    found, carried, base = 0, 0, 0
    pending: set[int] = set()
    for window in file_windows(path, overlap, chunk_bytes=chunk_bytes):
        hay = window.lower() if any_case else window
        for needle in wanted:
            at = hay.find(needle, max(0, carried - len(needle) + 1))
            while at >= 0:
                pending.add(base + at)
                at = hay.find(needle, at + 1)
        carried = min(overlap, len(window))
        base += len(window) - carried  # where the next window starts
        found += sum(1 for at in pending if at < base)
        pending = {at for at in pending if at >= base}
    return found + len(pending)


def beliefs_holding(conn: sqlite3.Connection, text: str, *, any_case: bool = False) -> int:
    """Beliefs, in any status, whose subject, relation, value or source holds ``text``, matched
    literally and case-sensitively, or with ASCII case folded as SQLite folds it. A prune redacts
    these (:func:`memware.ledger.redact`), so after one this counts what it could not reach, such
    as a text past the start of a field that ``--turns-starting-with`` left."""
    col = "lower(coalesce({}, ''))" if any_case else "coalesce({}, '')"
    arg = "lower(?1)" if any_case else "?1"
    where = " OR ".join(
        f"instr({col.format(c)}, {arg}) > 0" for c in ("subject", "relation", "value", "source")
    )
    return int(conn.execute(f"SELECT count(*) FROM belief WHERE {where}", (text,)).fetchone()[0])


def other_rows_holding(conn: sqlite3.Connection, text: str) -> dict[str, int]:
    """``table.column`` -> rows holding ``text``, for every ordinary table but ``turn`` and
    ``belief``: a retraction's reason, a review's, a cursor's source path, a passage. Only columns
    that hold it are listed."""
    out: dict[str, int] = {}
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' AND name NOT IN ('turn', 'belief')"
    ).fetchall()
    for (table,) in tables:
        if any(table.startswith(fts) and table[len(fts) :] in _FTS_SHADOW for fts in FTS_TABLES):
            continue
        for column in [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]:
            n = conn.execute(
                f'SELECT count(*) FROM "{table}" WHERE instr("{column}", ?) > 0', (text,)
            ).fetchone()[0]
            if n:
                out[f"{table}.{column}"] = int(n)
    return out


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
    carries one leading byte, ``0`` for the main index. A malformed page raises IndexError."""
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


def _pages(conn: sqlite3.Connection, table: str) -> Iterator[bytes]:
    """The leaf pages of an FTS5 index. The cursor is closed however the caller stops, so no
    statement stays open to block a VACUUM on the same connection."""
    rows = conn.execute(f"SELECT id, block FROM {table}_data WHERE id >= ?", (_LEAF_ROWID_MIN,))
    try:
        for rowid, page in rows:
            if not (rowid >> 31) & 0x3F:  # set bits mark a doclist-index page, not a leaf
                yield bytes(page)
    finally:
        rows.close()


def index_holds(conn: sqlite3.Connection, table: str, terms: list[str]) -> set[str]:
    """Which of ``terms`` appear on a leaf page of the FTS5 ``table``, deleted entries included.
    Stops reading once every term is found."""
    wanted = {b"0" + t.encode("utf-8"): t for t in terms}
    held: set[str] = set()
    for page in _pages(conn, table):
        held.update(wanted[t] for t in leaf_terms(page) if t in wanted)
        if len(held) == len(wanted):
            break
    return held


def live_rows(conn: sqlite3.Connection, table: str, terms: list[str] | None) -> dict[str, int]:
    """Term -> how many live rows the index holds it for, from ``fts5vocab`` in the connection's own
    temp schema, which never touches the store file. With ``terms``, only those are looked up, and
    a term no live row holds is left out."""
    vocab = f"temp.memware_vocab_{table}"
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {vocab} USING fts5vocab(main, {table}, row)")
    try:
        if terms is None:
            return {str(t): int(n) for t, n in conn.execute(f"SELECT term, doc FROM {vocab}")}
        found = (
            conn.execute(f"SELECT doc FROM {vocab} WHERE term=?", (t,)).fetchone() for t in terms
        )
        return {t: int(row[0]) for t, row in zip(terms, found, strict=True) if row}
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {vocab}")


def deleted_terms(conn: sqlite3.Connection, table: str, terms: list[str] | None = None) -> int:
    """How many terms sit on ``table``'s index pages that no live row holds: what deletes left for
    a merge to drop. With ``terms``, only those are counted; without, every term on every page."""
    if terms is None:
        on_pages = {
            t[1:].decode("utf-8", "replace")
            for p in _pages(conn, table)
            for t in leaf_terms(p)
            if t[:1] == b"0"
        }
    else:
        on_pages = index_holds(conn, table, terms)
    if not on_pages:
        return 0
    return len(
        on_pages - live_rows(conn, table, sorted(on_pages) if terms is not None else None).keys()
    )


@dataclass(frozen=True)
class FileCheck:
    """What one SQLite file, and the ``-wal`` beside it, hold of a value."""

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
    turns_any_case: int | None = None
    """Turns whose text holds it with ASCII case folded, those above included."""
    beliefs: int | None = None
    """Beliefs, in any status, whose subject, relation or value holds it."""
    beliefs_any_case: int | None = None
    """Beliefs holding it with ASCII case folded, those above included."""
    other_rows: dict[str, int] = field(default_factory=dict)
    """``table.column`` -> rows of any other table holding it (see :func:`other_rows_holding`)."""
    tokens: int = 0
    """How many search terms the value makes, as the store's tokenizer splits it."""
    index_tokens: dict[str, int] = field(default_factory=dict)
    """FTS table -> how many of those terms its index pages hold, deleted entries included."""
    deleted_tokens: dict[str, int] = field(default_factory=dict)
    """FTS table -> how many of those terms are on its pages only for a deleted row."""
    live_term_rows: dict[str, int] = field(default_factory=dict)
    """FTS table -> how many live rows its index holds every one of those terms for: a turn or a
    belief that holds the value in another case, or a word it shares a stem with."""
    term_is_value: bool = False
    """Whether the value is its own single search term, so an index page holding the term for a
    live row holds the value's bytes too."""
    free_pages: int | None = None
    """Pages the file holds and no table uses. Not searched as index pages; VACUUM drops them."""
    error: str | None = None
    """Why the file could not be queried; the byte counts above still stand."""

    @property
    def rows(self) -> int:
        """Live rows holding the value, all tables together."""
        return (self.turns or 0) + (self.beliefs or 0) + sum(self.other_rows.values())

    @property
    def found(self) -> bool:
        counted = (
            self.occurrences,
            self.occurrences_any_case,
            self.wal_occurrences,
            self.wal_occurrences_any_case,
            self.rows,
            *self.deleted_tokens.values(),
        )
        whole = bool(self.tokens) and self.tokens in self.index_tokens.values()
        return whole or any(counted)

    @property
    def leftover(self) -> bool:
        """Copies no live row accounts for: index terms of deleted rows, or the value's bytes in
        the file or its log while no row holds them. A scrub removes these; it cannot remove a row.

        A live row accounts for the bytes when it holds the value, or when the value is its own
        search term and the index keeps that term for a live row: pruning ``hunter2`` leaves the
        term ``hunter2`` on an index page for a turn that says ``Hunter2``, and that is not a copy
        of what was removed. A file that could not be queried counts as holding copies when its
        bytes hold the value."""
        stray_bytes = bool(self.occurrences or self.wal_occurrences)
        indexed_live = self.term_is_value and any(self.live_term_rows.values())
        accounted = bool(self.rows) or indexed_live
        return any(self.deleted_tokens.values()) or (
            stray_bytes and (self.error is not None or not accounted)
        )


def _open_readonly(path: Path) -> sqlite3.Connection:
    """A connection that cannot write the file or its log, nor checkpoint one into the other. With
    no ``-wal`` beside the file it is ``immutable``, which also creates no side file; with one it is
    ``mode=ro``, which reads the log's content and touches only the ``-shm`` index."""
    uri = path.as_uri()
    wal = Path(f"{path}-wal").exists()
    return sqlite3.connect(f"{uri}?mode=ro" if wal else f"{uri}?mode=ro&immutable=1", uri=True)


def check_file(
    path: str | os.PathLike[str],
    value: str,
    tokens: list[str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> FileCheck:
    """Count ``value`` in one SQLite file and its ``-wal``, then query it: rows holding the value,
    its search terms on the index pages, and which of those only a deleted row left. The path is
    resolved first, since SQLite keeps the ``-wal`` beside a symlink's target.

    The bytes are counted before anything is opened. ``conn`` queries through a connection the
    caller already holds; otherwise a read-only one is opened (:func:`_open_readonly`). An OSError
    reading the bytes propagates; an SQLite error leaves the counts and sets ``error``."""
    p = Path(path).expanduser().resolve()
    tokens = value_tokens(value) if tokens is None else tokens
    needle = value.encode("utf-8")
    wal = Path(f"{p}-wal")
    wal_counts = (
        (file_occurrences(wal, [needle]), file_occurrences(wal, [needle], any_case=True))
        if wal.exists()
        else (None, None)
    )
    base = FileCheck(
        str(p),
        file_occurrences(p, [needle]),
        file_occurrences(p, [needle], any_case=True),
        wal_counts[0],
        wal_counts[1],
        tokens=len(tokens),
    )
    try:
        con = conn or _open_readonly(p)
        try:
            q = con.execute
            exact, folded = q(
                "SELECT coalesce(sum(instr(text, ?1) > 0), 0), "
                "coalesce(sum(instr(lower(text), lower(?1)) > 0), 0) FROM turn",
                (value,),
            ).fetchone()
            live = {t: live_rows(con, t, tokens) for t in FTS_TABLES}
            return FileCheck(
                base.path,
                base.occurrences,
                base.occurrences_any_case,
                base.wal_occurrences,
                base.wal_occurrences_any_case,
                turns=int(exact),
                turns_any_case=int(folded),
                beliefs=beliefs_holding(con, value),
                beliefs_any_case=beliefs_holding(con, value, any_case=True),
                other_rows=other_rows_holding(con, value),
                tokens=len(tokens),
                index_tokens={t: len(index_holds(con, t, tokens)) for t in FTS_TABLES},
                deleted_tokens={t: deleted_terms(con, t, tokens) for t in FTS_TABLES},
                live_term_rows={
                    t: min(docs.values()) if tokens and len(docs) == len(tokens) else 0
                    for t, docs in live.items()
                },
                term_is_value=tokens == [value],
                free_pages=int(q("PRAGMA freelist_count").fetchone()[0]),
            )
        finally:
            if conn is None:
                con.close()
    except (sqlite3.Error, IndexError) as e:
        return replace(base, error=f"{type(e).__name__}: {e}")
