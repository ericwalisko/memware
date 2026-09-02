"""Ingest adapters: turn transcript files into indexed turns, idempotently.

Every adapter yields :class:`Turn` records and is driven by :func:`sync_file`,
which keeps a byte-offset cursor per source file so a re-run appends only what
is new. Transcripts themselves are never modified.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from memware.store import Store, now_iso


@dataclass(frozen=True)
class Turn:
    session: str
    ts: str | None
    role: str
    text: str


Parser = Callable[[Path, int], Iterator[tuple[int, Turn]]]
"""A parser takes (path, start_offset) and yields (offset_after_line, turn)."""

_REGISTRY: dict[str, Parser] = {}


def register(harness: str, parser: Parser) -> None:
    _REGISTRY[harness] = parser


def parser_for(harness: str) -> Parser:
    try:
        return _REGISTRY[harness]
    except KeyError as e:
        raise KeyError(f"unknown harness {harness!r}; known: {sorted(_REGISTRY)}") from e


def sync_file(store: Store, path: str | os.PathLike[str], *, harness: str) -> int:
    """Index new turns from one transcript file. Returns the number added."""
    p = Path(path)
    source = str(p.resolve())
    parse = parser_for(harness)
    row = store.conn.execute("SELECT offset, seq FROM cursor WHERE source=?", (source,)).fetchone()
    offset, seq = (int(row["offset"]), int(row["seq"])) if row else (0, 0)
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        if p.stat().st_size < offset:  # truncated or rewritten: start over for this source
            offset, seq = 0, 0
            conn.execute("DELETE FROM turn WHERE source=?", (source,))
        added = 0
        for offset_after, turn in parse(p, offset):
            seq += 1
            conn.execute(
                "INSERT OR IGNORE INTO turn(session,seq,ts,role,text,source,harness) "
                "VALUES (?,?,?,?,?,?,?)",
                (turn.session, seq, turn.ts, turn.role, turn.text, source, harness),
            )
            added += 1
            offset = offset_after
        conn.execute(
            "INSERT INTO cursor(source,offset,seq,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET offset=excluded.offset, seq=excluded.seq, "
            "updated_at=excluded.updated_at",
            (source, offset, seq, now_iso()),
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")
    return added


def sync_tree(
    store: Store, root: str | os.PathLike[str], *, harness: str, glob: str = "**/*.jsonl"
) -> dict[str, int]:
    """Sync every matching file under ``root``. Returns {path: added}."""
    out: dict[str, int] = {}
    for p in sorted(Path(root).expanduser().glob(glob)):
        if p.is_file():
            out[str(p)] = sync_file(store, p, harness=harness)
    return out


from memware.ingest import claude_code as _cc  # noqa: E402
from memware.ingest import generic as _generic  # noqa: E402

__all__ = ["Parser", "Turn", "parser_for", "register", "sync_file", "sync_tree"]
_ = (_cc, _generic)
