"""Ingest adapters: turn transcript files into indexed turns, idempotently.

Every adapter yields :class:`Turn` records and is driven by :func:`sync_file`,
which keeps a byte-offset cursor per source file so a re-run appends only what
is new. Transcripts themselves are never modified.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from memware.passage import index_turn
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


IGNORE_MARKERS_ENV = "MEMWARE_IGNORE_MARKERS"
"""Newline- or ``os.pathsep``-separated content markers. Any transcript whose head contains
one is never indexed by any sync — the durable defence against contamination from runs that
predate a marker or the no-capture flag. Unioned with the file below and any per-call marker."""


def ignore_markers_file() -> Path:
    """Resolved per call, so ``MEMWARE_HOME`` set after import is still honoured."""
    return Path(os.environ.get("MEMWARE_HOME", "~/.memware")).expanduser() / "ignore-markers.txt"


def default_skip_markers() -> list[str]:
    """Persistent skip markers from the env var and the ignore-markers file (deduped)."""
    out: list[str] = []
    raw = os.environ.get(IGNORE_MARKERS_ENV, "")
    for part in raw.replace(os.pathsep, "\n").splitlines():
        if part.strip():
            out.append(part.strip())
    try:
        for line in ignore_markers_file().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    except OSError:
        pass
    seen: list[str] = []
    for m in out:
        if m not in seen:
            seen.append(m)
    return seen


NO_CAPTURE_ENV = "MEMWARE_NO_CAPTURE"
"""Set to 1 in the environment of an agent run you do not want indexed (evaluations,
benchmarks, throwaway experiments). The variable reaches only the processes that session
starts, so every ``--from-hook`` command that sees it puts the session's transcript on the
no-capture list (:func:`record_no_capture`), and every sync and backup honours the list
whatever environment it runs in. The Hermes provider captures nothing under it."""


def capture_disabled() -> bool:
    return os.environ.get(NO_CAPTURE_ENV, "").strip().lower() in ("1", "true", "yes")


def no_capture_file() -> Path:
    """``<memware home>/no-capture.txt``: one resolved transcript path per line."""
    from memware.config import memware_home

    return memware_home() / "no-capture.txt"


def no_capture_paths() -> set[str]:
    """Transcripts put on the no-capture list. No sync indexes them and no backup mirrors them."""
    try:
        text = no_capture_file().read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def is_no_capture(source: str | os.PathLike[str], listed: set[str] | None = None) -> bool:
    """Whether a resolved transcript path is on the no-capture list, or inside the directory
    Claude Code keeps beside a listed transcript. A session's subagents write their own
    transcripts under ``<session>/subagents/``, next to ``<session>.jsonl``, and their turns
    carry the session's id."""
    listed = no_capture_paths() if listed is None else listed
    if not listed:
        return False
    p = Path(source)
    return str(p) in listed or any(f"{d}.jsonl" in listed for d in p.parents)


@contextmanager
def _exclusive(lock: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``lock`` for the block; unlocked where there is no flock."""
    try:
        import fcntl
    except ImportError:  # Windows
        yield
        return
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def record_no_capture(path: str | os.PathLike[str]) -> bool:
    """Put a transcript on the no-capture list. Returns False if it was already there.

    ``MEMWARE_NO_CAPTURE`` lives in one session's environment, and the catch-up sync and the
    backup mirror run in other processes that never see it. The list carries the decision to
    them. The path is resolved the way :func:`sync_file` resolves a source, and it need not
    exist yet: a session-start hook records a transcript before its first line is written.
    Writers take a lock beside the file and replace the file whole, so sessions that start
    together all land and a reader never sees half a line."""
    source = str(Path(path).expanduser().resolve())
    target = no_capture_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive(target.with_name(target.name + ".lock")):
        try:
            listed = [
                line.strip()
                for line in target.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except FileNotFoundError:
            listed = []
        if source in listed:
            return False
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text("".join(f"{p}\n" for p in [*listed, source]), encoding="utf-8")
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    return True


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
    evidence it is evaluated against. The persistent markers from
    :func:`default_skip_markers` always apply on top of it, and a file on the no-capture
    list (:func:`record_no_capture`), or one of its subagents' transcripts, is skipped and
    un-indexed the same way.
    """
    p = Path(path)
    source = str(p.resolve())
    markers = default_skip_markers()
    if skip_if_contains:
        markers += (
            [skip_if_contains] if isinstance(skip_if_contains, str) else list(skip_if_contains)
        )
    if is_no_capture(source) or (markers and file_contains(p, markers)):
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
            cur = conn.execute(
                "INSERT OR IGNORE INTO turn(session,seq,ts,role,text,source,harness) "
                "VALUES (?,?,?,?,?,?,?)",
                (turn.session, seq, turn.ts, turn.role, turn.text, source, harness),
            )
            if cur.rowcount:  # ignored rows are a re-read of the same (source, seq)
                index_turn(conn, int(cur.lastrowid or 0), turn.text)
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


def prune_turns(store: Store, *, containing: str) -> int:
    """Delete individual turns whose text starts with ``containing`` (a boilerplate prefix),
    leaving the rest of each session indexed. FTS stays in sync via the delete trigger.

    Unlike :func:`prune_sources` (which drops whole transcripts), this is turn-level — the
    right tool for harness boilerplate that recurs inside otherwise-real sessions, such as a
    skill preamble already indexed before the parser learned to skip it."""
    cur = store.conn.execute("DELETE FROM turn WHERE text LIKE ?", (containing + "%",))
    return int(cur.rowcount)


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
    "default_skip_markers",
    "file_contains",
    "ignore_markers_file",
    "is_no_capture",
    "no_capture_file",
    "no_capture_paths",
    "parser_for",
    "prune_source",
    "prune_sources",
    "record_no_capture",
    "register",
    "sync_file",
    "sync_tree",
]
_ = (_cc, _generic)
