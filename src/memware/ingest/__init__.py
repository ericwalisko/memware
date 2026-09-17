"""Ingest adapters: turn transcript files into indexed turns, idempotently.

Every adapter yields :class:`Turn` records and is driven by :func:`sync_file`,
which keeps a byte-offset cursor per source file so a re-run appends only what
is new. Transcripts themselves are never modified.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from memware.ledger import Retraction, apply_retraction, plan_retraction
from memware.passage import index_turn
from memware.residue import FileCheck, beliefs_holding, check_file, file_windows, value_tokens
from memware.store import Scrubbed, Store, now_iso

WITHHELD = "(value withheld)"
"""What a prune writes in place of its text wherever it records itself, such as a retraction's
reason. The text is usually a secret, and writing it back would undo the prune."""


@dataclass(frozen=True)
class Turn:
    session: str
    ts: str | None
    role: str
    text: str
    entrypoint: str | None = None
    """What started the session, when the transcript says (``cli``, ``sdk-cli`` …). A parser
    that cannot know leaves it None, and derive reads such a turn as interactive."""


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


def capture_exclude_patterns() -> list[str]:
    """``capture.exclude`` from the config: path globs whose transcripts no sync indexes and no
    backup mirrors. Read per call, like the skip markers, so an edit applies at the next sync.

    The no-capture list needs a hook to run in the session and a marker needs the text inside
    the transcript. A pattern needs neither: it lives in the machine's config, so a script that
    forgets ``MEMWARE_NO_CAPTURE`` is still excluded by where its sessions run. A bare string
    counts as one pattern, which is what ``memware config capture.exclude GLOB`` writes."""
    from memware.config import get_dotted, load_config

    raw = get_dotted(load_config(), "capture.exclude")
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [p.strip() for p in items if isinstance(p, str) and p.strip()]


def matches_exclude(source: str | os.PathLike[str], pattern: str) -> bool:
    """Whether a resolved transcript path matches one ``capture.exclude`` pattern.

    The whole path is matched with :func:`fnmatch.fnmatch`, as ``memware prune --glob`` matches,
    so ``*`` crosses ``/``: ``*/-Users-me-gen/*`` names one Claude Code project directory, every
    session in it, and those sessions' subagents. A leading ``~`` is expanded."""
    import fnmatch

    return fnmatch.fnmatch(str(source), os.path.expanduser(pattern))


def is_excluded(source: str | os.PathLike[str], patterns: list[str] | None = None) -> bool:
    """Whether a resolved transcript path matches any ``capture.exclude`` pattern."""
    patterns = capture_exclude_patterns() if patterns is None else patterns
    return any(matches_exclude(source, pat) for pat in patterns)


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


def _file_holds(path: Path, marker: str, *, chunk_bytes: int = 1 << 20) -> bool:
    """True if ``marker`` appears anywhere in the file, which is read whole, a chunk at a time.

    Unlike :func:`file_contains`, a head check, this is the ground truth ``prune --containing``
    decides by. A marker that spans a chunk boundary still matches (:func:`file_windows`)."""
    needle = marker.encode("utf-8")
    return any(needle in w for w in file_windows(path, len(needle) - 1, chunk_bytes=chunk_bytes))


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
    list (:func:`record_no_capture`), or one of its subagents' transcripts, or one whose path
    matches a ``capture.exclude`` pattern (:func:`is_excluded`), is skipped and un-indexed the
    same way.
    """
    p = Path(path)
    source = str(p.resolve())
    markers = default_skip_markers()
    if skip_if_contains:
        markers += (
            [skip_if_contains] if isinstance(skip_if_contains, str) else list(skip_if_contains)
        )
    if is_no_capture(source) or is_excluded(source) or (markers and file_contains(p, markers)):
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
                "INSERT OR IGNORE INTO turn(session,seq,ts,role,text,source,harness,entrypoint) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    turn.session,
                    seq,
                    turn.ts,
                    turn.role,
                    turn.text,
                    source,
                    harness,
                    turn.entrypoint,
                ),
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

    ``exclude`` is a list of glob patterns matched against the full path (fnmatch). It holds for
    this call only and un-indexes nothing; the persistent ``capture.exclude`` patterns are
    honoured by :func:`sync_file`, which also un-indexes what they match."""
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
    """Remove every turn and the cursor for one indexed source path. Returns turns removed.

    Beliefs are left alone. ``sync_file`` calls this for every listed or marked transcript, from
    hooks, and a belief changes only after someone has read a dry run: ``memware stats`` counts
    the beliefs this can leave citing a missing session, and ``memware beliefs retract
    --orphaned`` retracts them. :func:`prune` is the un-index that cascades."""
    n = int(store.conn.execute("SELECT count(*) FROM turn WHERE source=?", (source,)).fetchone()[0])
    store.conn.execute("DELETE FROM turn WHERE source=?", (source,))
    store.conn.execute("DELETE FROM cursor WHERE source=?", (source,))
    return n


@dataclass(frozen=True)
class Pruned:
    """What :func:`prune` removed, or would remove without ``apply``."""

    sources: dict[str, int]
    """Source path -> turns, for a whole-source prune; empty for a turn-level one."""
    turns: int
    beliefs: Retraction
    """The retraction it cascades into: beliefs from every session it leaves with no turn."""
    applied: bool
    scanned: int = 0
    """Transcript files ``containing`` read whole; 0 when it was not given."""
    missing: tuple[str, ...] = ()
    """Sources ``containing`` could not read because the transcript file is gone. Their turns
    may still be indexed, and only a turn selector reaches them."""
    beliefs_holding: int | None = None
    """Beliefs, in any status, whose subject, relation or value holds the selector's text, matched
    as the selector matches. Prune keeps every belief row, so their text stays in the store file.
    None for a ``glob`` alone, which has no text."""
    reasons_redacted: int = 0
    """Retraction reasons that held the text and now read :data:`WITHHELD` in its place. A prune in
    memware 0.6.0 and 0.6.1 wrote its whole command line into each reason."""
    scrubbed: Scrubbed | None = None
    """How the store file was rewritten. None for a dry run, a scrub that failed, or an applied
    prune that removed nothing and found no copy of its text left to scrub."""
    scrub_error: str | None = None
    """Why the scrub did not finish. The removal had already committed, so the removed text is out
    of every query and may still be in the file."""
    left: FileCheck | None = None
    """What the store file and its log still hold of the text once an applied prune is done. None
    for a dry run, a ``glob`` alone, or a store with no file."""


@dataclass(frozen=True)
class TurnMatches:
    """How a text occurs across every indexed turn. Matching is literal and case-sensitive, as
    :func:`prune` matches, except ``containing_any_case`` (ASCII case only, as SQLite folds it)."""

    turns: int
    starting_with: int
    containing: int
    containing_any_case: int


def turn_matches(store: Store, text: str) -> TurnMatches:
    """Count the turns that start with ``text``, contain it and contain it in any case: what a
    turn selector that matched nothing can say instead of a bare zero. One pass over the table."""
    row = store.conn.execute(
        "SELECT count(*), coalesce(sum(instr(text, ?1) = 1), 0), "
        "coalesce(sum(instr(text, ?1) > 0), 0), "
        "coalesce(sum(instr(lower(text), lower(?1)) > 0), 0) FROM turn",
        (text,),
    ).fetchone()
    return TurnMatches(*(int(v) for v in row))


def _matching_sources(
    store: Store, glob: str | None, containing: str | None
) -> tuple[list[str], int, list[str]]:
    """The indexed sources ``glob`` and ``containing`` select, how many transcript files
    ``containing`` read, and the selected sources whose file is gone."""
    import fnmatch

    out: list[str] = []
    missing: list[str] = []
    scanned = 0
    for (source,) in store.conn.execute("SELECT source FROM cursor").fetchall():
        if glob and not fnmatch.fnmatch(source, glob):
            continue
        if containing:
            try:
                held = _file_holds(Path(source), containing)
            except FileNotFoundError:
                missing.append(source)
                continue
            scanned += 1
            if not held:
                continue
        out.append(source)
    return out, scanned, missing


def prune(
    store: Store,
    *,
    glob: str | None = None,
    containing: str | None = None,
    turns_containing: str | None = None,
    turns_starting_with: str | None = None,
    apply: bool = False,
    reason: str = "memware prune",
    progress: Callable[[str], None] | None = None,
) -> Pruned:
    """Un-index whole sources (``glob`` and/or ``containing``) or single turns
    (``turns_containing`` or ``turns_starting_with``), and retract the beliefs derived from every
    session that leaves with no turn indexed (see :func:`memware.ledger.plan_retraction`).

    ``containing`` reads every transcript file whole. ``turns_containing`` matches a turn holding
    the text anywhere, ``turns_starting_with`` only one that begins with it. Both match
    literally and case-sensitively, and a turn selector takes no other selector.

    Without ``apply`` nothing is written and the result is what would happen. With it, the
    deletes, the retraction, and the rewrite of any retraction reason holding the text commit
    together. Sessions are read before the delete: a deleted turn no longer says which session it
    came from. ``reason`` is recorded as it is given, so it must not hold the text.

    An applied prune then scrubs the store file (:meth:`memware.store.Store.scrub`), reporting each
    step to ``progress``, when it removed anything, or when its text is still in the file with no
    live row to account for it, as a prune in memware 0.6.1 and earlier left it. A scrub that fails is
    reported in ``scrub_error``, not raised. Last, ``left`` checks what the file still holds."""
    missing: list[str] = []
    scanned = 0
    turn_selectors = [t for t in (turns_containing, turns_starting_with) if t is not None]
    if turn_selectors and (len(turn_selectors) > 1 or glob or containing):
        raise ValueError("a turn selector takes no other selector")
    if turn_selectors and not turn_selectors[0]:
        raise ValueError("an empty turn selector would match every turn")
    text = turns_containing or turns_starting_with or containing
    holding = None if text is None else beliefs_holding(store.conn, text)
    if turns_containing is not None:
        sources: list[str] = []
        doomed, args = "instr(text, ?) > 0", [turns_containing]
    elif turns_starting_with is not None:
        sources = []
        doomed, args = "instr(text, ?) = 1", [turns_starting_with]
    else:
        sources, scanned, missing = _matching_sources(store, glob, containing)
        doomed, args = f"source IN ({','.join('?' * len(sources))})", sources
    conn = store.conn
    redacted = 0
    conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
    try:
        counts = dict.fromkeys(sources, 0)
        for source, n in conn.execute(
            f"SELECT source, count(*) FROM turn WHERE {doomed} GROUP BY source", args
        ):
            counts[source] = int(n)
        turns = int(conn.execute(f"SELECT count(*) FROM turn WHERE {doomed}", args).fetchone()[0])
        touched = [
            r[0] for r in conn.execute(f"SELECT DISTINCT session FROM turn WHERE {doomed}", args)
        ]
        if apply:
            conn.execute(f"DELETE FROM turn WHERE {doomed}", args)
            if sources:
                conn.execute(f"DELETE FROM cursor WHERE {doomed}", args)
        # Once applied no doomed turn is left, so one test serves both modes: a session is
        # emptied when no turn the prune spares remains in it.
        emptied = [
            s
            for s in touched
            if conn.execute(
                f"SELECT 1 FROM turn WHERE session=? AND NOT ({doomed}) LIMIT 1", [s, *args]
            ).fetchone()
            is None
        ]
        plan = plan_retraction(store, emptied)
        if apply:
            apply_retraction(store, plan, reason=reason)
            if text:
                # as given, and as a 0.6.x prune's command line quoted it (repr escapes a backslash)
                for form in dict.fromkeys([text, repr(text)[1:-1]]):
                    redacted += conn.execute(
                        "UPDATE retraction SET reason = replace(reason, ?1, ?2) "
                        "WHERE instr(reason, ?1) > 0",
                        (form, WITHHELD),
                    ).rowcount
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")
    result = Pruned(
        counts if sources else {},
        turns,
        plan,
        apply,
        scanned,
        tuple(missing),
        holding,
        redacted,
    )
    if not apply:
        return result
    return _scrub_after(store, result, text, bool(turns or sources or redacted), progress)


def _scrub_after(
    store: Store,
    result: Pruned,
    text: str | None,
    removed: bool,
    progress: Callable[[str], None] | None,
) -> Pruned:
    """The scrub and the check that follow an applied prune: see :func:`prune`."""
    on_disk = text is not None and str(store.path) != ":memory:"
    tokens = value_tokens(text) if text is not None and on_disk else []
    before = None
    if not removed and text is not None and on_disk:
        before = check_file(store.path, text, tokens, conn=store.conn)
    if not removed and not (before and before.leftover):
        return replace(result, left=before)
    scrubbed, error = None, None
    try:
        scrubbed = store.scrub(progress)
    except (sqlite3.Error, OSError) as e:
        error = f"{type(e).__name__}: {e}"
    left = (
        check_file(store.path, text, tokens, conn=store.conn)
        if text is not None and on_disk
        else None
    )
    return replace(result, scrubbed=scrubbed, scrub_error=error, left=left)


def prune_turns(
    store: Store, *, containing: str | None = None, starting_with: str | None = None
) -> int:
    """Delete individual turns whose text contains ``containing`` anywhere, or starts with
    ``starting_with`` (a boilerplate prefix), leaving the rest of each session indexed. FTS
    stays in sync via the delete trigger. Give exactly one.

    Unlike :func:`prune_sources` (which drops whole transcripts), this is turn-level: the tool
    for a value pasted mid-session, and with ``starting_with`` for harness boilerplate that
    recurs inside otherwise-real sessions, such as a skill preamble already indexed before the
    parser learned to skip it. A session left with no turn has its derived beliefs retracted,
    as :func:`prune` does with ``apply``."""
    if (containing is None) == (starting_with is None):
        raise ValueError("prune_turns takes exactly one of containing and starting_with")
    return prune(
        store, turns_containing=containing, turns_starting_with=starting_with, apply=True
    ).turns


def prune_sources(
    store: Store, *, glob: str | None = None, containing: str | None = None
) -> dict[str, int]:
    """Un-index sources whose path matches ``glob`` and/or whose file contains ``containing``,
    and retract the beliefs derived from the sessions that leaves with no turn: :func:`prune`
    with ``apply``."""
    return prune(store, glob=glob, containing=containing, apply=True).sources


from memware.ingest import claude_code as _cc  # noqa: E402
from memware.ingest import generic as _generic  # noqa: E402

__all__ = [
    "NO_CAPTURE_ENV",
    "Parser",
    "Pruned",
    "Turn",
    "TurnMatches",
    "capture_disabled",
    "capture_exclude_patterns",
    "default_skip_markers",
    "file_contains",
    "ignore_markers_file",
    "is_excluded",
    "is_no_capture",
    "matches_exclude",
    "no_capture_file",
    "no_capture_paths",
    "parser_for",
    "prune",
    "prune_source",
    "prune_sources",
    "prune_turns",
    "record_no_capture",
    "register",
    "sync_file",
    "sync_tree",
    "turn_matches",
]
_ = (_cc, _generic)
