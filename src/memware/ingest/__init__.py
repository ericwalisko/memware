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


NO_CAPTURE_ENV = "MEMWARE_NO_CAPTURE"
"""Set to 1 in the environment of an agent run you do not want indexed (evaluations,
benchmarks, throwaway experiments). Hooks and providers honour it; so does ``sync``."""


def capture_disabled() -> bool:
    return os.environ.get(NO_CAPTURE_ENV, "").strip().lower() in ("1", "true", "yes")


def file_contains(path: Path, marker: str | list[str], *, head_bytes: int = 200_000) -> bool:
    """True if any ``marker`` appears in the first ``head_bytes`` of the file (cheap pre-filter)."""
    markers = [marker] if isinstance(marker, str) else [m for m in marker if m]
    if not markers:
        return False
    with path.open("rb") as fh:
        head = fh.read(head_bytes)
    return any(m.encode("utf-8") in head for m in markers)


def sync_file(
    store: Store,
    path: str | os.PathLike[str],
    *,
    harness: str,
    skip_if_contains: str | list[str] | None = None,
) -> int:
    """Index new turns from one transcript file. Returns the number added.

    ``skip_if_contains`` skips (and un-indexes, if previously indexed) any file whose
    head contains the marker — the way to keep an evaluation's own sessions out of the
    evidence it is evaluated against.
    """
    p = Path(path)
    source = str(p.resolve())
    if skip_if_contains and file_contains(p, skip_if_contains):
        prune_source(store, source)
        return 0
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
    store: Store,
    root: str | os.PathLike[str],
    *,
    harness: str,
    glob: str = "**/*.jsonl",
    skip_if_contains: str | list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, int]:
    """Sync every matching file under ``root``. Returns {path: added}.

    ``exclude`` is a list of glob patterns matched against the full path (fnmatch)."""
    import fnmatch

    out: dict[str, int] = {}
    for p in sorted(Path(root).expanduser().glob(glob)):
        if not p.is_file():
            continue
        if exclude and any(fnmatch.fnmatch(str(p), pat) for pat in exclude):
            continue
        out[str(p)] = sync_file(store, p, harness=harness, skip_if_contains=skip_if_contains)
    return out


def prune_source(store: Store, source: str) -> int:
    """Remove every turn and the cursor for one indexed source path. Returns turns removed."""
    n = int(store.conn.execute("SELECT count(*) FROM turn WHERE source=?", (source,)).fetchone()[0])
    store.conn.execute("DELETE FROM turn WHERE source=?", (source,))
    store.conn.execute("DELETE FROM cursor WHERE source=?", (source,))
    return n


def prune_sources(
    store: Store, *, glob: str | None = None, containing: str | None = None
) -> dict[str, int]:
    """Un-index sources whose path matches ``glob`` and/or whose file contains ``containing``."""
    import fnmatch

    out: dict[str, int] = {}
    for (source,) in store.conn.execute("SELECT source FROM cursor").fetchall():
        if glob and not fnmatch.fnmatch(source, glob):
            continue
        if containing:
            p = Path(source)
            if not (p.exists() and file_contains(p, containing)):
                continue
        out[source] = prune_source(store, source)
    return out


from memware.ingest import claude_code as _cc  # noqa: E402
from memware.ingest import generic as _generic  # noqa: E402

__all__ = [
    "NO_CAPTURE_ENV",
    "Parser",
    "Turn",
    "capture_disabled",
    "file_contains",
    "parser_for",
    "prune_source",
    "prune_sources",
    "register",
    "sync_file",
    "sync_tree",
]
_ = (_cc, _generic)
