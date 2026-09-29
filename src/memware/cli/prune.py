"""`memware prune`: un-index sources or turns, retract what they left orphaned, scrub the store file; and the cascade/scrub reporting other commands reuse."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from memware.cli._common import _ASK, AddCommand, _emit, _n, _NoText, _out, _plural, _read_text
from memware.ingest import WITHHELD, Pruned, prune, turn_matches
from memware.ledger import (
    REDACT_MAX_BELIEFS,
    REDACT_MIN_CHARS,
    REDACTED,
    Redaction,
    RedactionRefused,
    Retraction,
    make_key,
)
from memware.residue import FileCheck
from memware.store import Scrubbed, Store

_CASCADE_COLS = [
    ("action", "action"),
    ("id", "id"),
    ("subject", "subject"),
    ("relation", "relation"),
    ("value", "value"),
    ("valid_from", "valid from"),
    ("valid_to", "valid to"),
    ("was_superseded_by", "was superseded by"),
    ("superseded_by", "superseded by"),
    ("reason", "reason"),
    ("source", "source"),
]


# JSON key -> (label once applied, label in a dry run), for the counts a prune leads with.
_PRUNE_COUNTS = {
    "sources_pruned": ("sources un-indexed", "sources to un-index"),
    "turns_removed": ("turns removed", "turns to remove"),
}


# The fixed label vocabulary a prune's default/--plain view prints -- memware's own words, never
# derived from a value in the store. The output guard (_Withheld) leaves these, and its REDACTED
# marker, alone: a --turns-containing/--containing text of a letter or two would otherwise match
# them too (e.g. "e" inside "beliefs", or inside "removed" itself) and mangle memware's own output
# along with the value it is actually withholding.
_PRUNE_LABELS = (
    *dict.fromkeys(lbl for pair in _PRUNE_COUNTS.values() for lbl in pair),
    *dict.fromkeys(lbl for _, lbl in _CASCADE_COLS),
    "sessions with no turn left",
    "beliefs retracted",
    "beliefs to retract",
    "predecessors reopened",
    "predecessors to reopen",
    "predecessors relinked",
    "predecessors to relink",
    "human-stated beliefs kept",
    "beliefs redacted",
    "beliefs to redact",
    "redaction guard",
    "store file",
    "text left in the store",
)


def _cascade(
    a: argparse.Namespace,
    plan: Retraction,
    applied: bool,
    head: dict[str, Any] | None = None,
    lines: list[tuple[str, str]] | None = None,
    *,
    by_session: bool = True,
    unwritten: str = "dry run: nothing written; add --apply to write it",
) -> None:
    """Print a belief retraction, done or planned: counts, then one record per belief it touches.

    Counts are labeled ``field : value`` lines like ``stats``, and records follow in the
    ``beliefs`` layout, each led by its action. The dry-run notice goes to stderr, so stdout stays
    data. --json carries ``head`` and the full lists; --plain prints only the records. The labeled
    view prints the ``head`` counts :data:`_PRUNE_COUNTS` names, and ``lines`` after the counts."""
    head = head or {}
    lists = {
        "retract": plan.retract,
        "reopen": plan.reopen,
        "relink": plan.relink,
        "keep": plan.kept,
    }
    records = [
        {"action": act, **row, "reason": plan.reasons.get(row["id"])}
        for act, rows in lists.items()
        for row in rows
    ]
    if not applied:
        print(unwritten, file=sys.stderr)
    if a.json:
        body = {"applied": applied, **head, "sessions_emptied": plan.sessions, **lists}
        if not by_session:
            body = {"applied": applied, "reasons": plan.reasons, **lists}
        print(json.dumps(body, indent=2, default=str))
        return
    if a.plain:
        _emit(a, records, _CASCADE_COLS)
        return
    n = 0 if applied else 1
    counts: list[tuple[str, int | str]] = [
        (_PRUNE_COUNTS[k][n], v) for k, v in head.items() if k in _PRUNE_COUNTS
    ]
    if by_session:
        counts.append(("sessions with no turn left", len(plan.sessions)))
    counts.append((("beliefs retracted", "beliefs to retract")[n], len(plan.retract)))
    if by_session:
        counts += [
            (("predecessors reopened", "predecessors to reopen")[n], len(plan.reopen)),
            (("predecessors relinked", "predecessors to relink")[n], len(plan.relink)),
            ("human-stated beliefs kept", len(plan.kept)),
        ]
    counts += lines or []
    width = max(len(label) for label, _ in counts)
    print("\n".join(f"{label.rjust(width)} : {_n(value)}" for label, value in counts))
    if records:
        print()
        _emit(a, records, _CASCADE_COLS)


_MATCHED = "matched literally and case-sensitively"


def _turn_notes(s: Store, text: str, *, prefix: bool, removed: int) -> list[str]:
    """Why a turn selector matched nothing, or, for a prefix, the turns it leaves holding the
    text past their start. A bare 0 reads as a clean store, so a note names where the text is. The
    text itself is never printed: it is usually a secret."""
    m = turn_matches(s, text)
    rest = m.containing - m.starting_with  # turns holding it past the start, which a prefix keeps
    if prefix and rest and removed:
        return [
            f"{_plural(rest, 'more turn', 'contain')} the text past the start and "
            f"{'is' if rest == 1 else 'are'} kept: --turns-containing selects them too"
        ]
    if prefix and rest:
        return [
            f"no turn starts with the text ({m.turns:,} searched, {_MATCHED}); "
            f"{_plural(rest, 'turn', 'contain')} it past the start: try --turns-containing"
        ]
    if removed:
        return []
    note = f"no turn {'starts with or contains' if prefix else 'contains'} the text "
    note += f"({m.turns:,} searched, {_MATCHED})"
    if m.containing_any_case:
        note += f"; {_plural(m.containing_any_case, 'turn', 'contain')} it in another case"
    return [note]


def _source_notes(s: Store, a: argparse.Namespace, r: Pruned) -> list[str]:
    """What a source selector that matched nothing searched, the transcripts ``--containing``
    could not read, and the indexed turns that still hold the text."""
    notes = []
    if not r.sources and a.containing:
        within = " matching --glob" if a.glob else ""
        notes.append(
            "no indexed source contains the text: "
            f"{_plural(r.scanned, 'transcript file')}{within} read whole, {_MATCHED}"
        )
    elif not r.sources:
        total = int(s.conn.execute("SELECT count(*) FROM cursor").fetchone()[0])
        notes.append(f"no path of the {_plural(total, 'indexed source')} matches {a.glob!r}")
    if r.missing:
        notes.append(
            f"{_plural(len(r.missing), 'indexed source')} not read: the transcript file is gone, "
            "and --turns-containing searches the turns still indexed from it"
        )
    if not r.sources and a.containing:
        found = turn_matches(s, a.containing).containing
        if found:
            notes.append(f"{_plural(found, 'indexed turn', 'contain')} it: try --turns-containing")
    return notes


def _redaction_line(red: Redaction, applied: bool) -> str:
    """How many beliefs a prune redacts and their ids: never the text."""
    if not red.beliefs:
        return "0"
    ids = ", ".join(str(i) for i in red.beliefs)
    retracted = (
        f"; {len(red.retracted):,} committed, {'now retracted' if applied else 'to retract'}"
        if red.retracted
        else ""
    )
    reviews = (
        f"; {_plural(len(red.reviews), 'open review')} {'closed' if applied else 'to close'}"
        if red.reviews
        else ""
    )
    return f"{len(red.beliefs):,} (ids {ids}){retracted}{reviews}"


def _scrubbed_line(r: Pruned) -> str:
    """How the store file was rewritten after an applied prune, or why it was not."""
    if r.scrub_error:
        return f"NOT scrubbed: {r.scrub_error}"
    done = r.scrubbed
    if done is None:
        return "not rewritten: nothing was removed, and no copy of the text was left to scrub"
    rebuilt = f", {' and '.join(done.rebuilt)} rebuilt" if done.rebuilt else ""
    log = "emptied" if done.wal_truncated else "NOT emptied, another process was reading"
    return (
        f"scrubbed in {done.seconds:.1f} s: search indexes merged{rebuilt}, compacted, "
        f"write-ahead log {log}"
    )


def _index_found(held: dict[str, int]) -> str:
    where = ", ".join(f"{table} {n:,}" for table, n in held.items() if n)
    return f"{_plural(sum(held.values()), 'term')} of deleted rows on the index pages ({where})"


def _index_left_line(r: Pruned) -> str:
    """What the check after a scrub with no text to look for found, or why it could not tell, as
    :func:`_left_line` words a text's check. The check reads the index as the store's connection
    sees it, the write-ahead log included. So it says nothing is left only when the scrub finished
    and emptied the log, and the file holds exactly the pages it read: with the log still full,
    the file may keep the pages the scrub rewrote."""
    held = r.index_left
    if held is None:
        return "not checked: the search index could not be read"
    if any(held.values()):
        return _index_found(held)
    if r.scrub_error is not None:
        return "not checked: the scrub did not finish, so the file may hold removed text"
    if r.scrubbed is not None and not r.scrubbed.wal_truncated:
        return (
            "not checked: another process was reading the store, so the rewritten pages are still "
            "in the write-ahead log and the file may hold the pages they replace"
        )
    return (
        "nothing: the file was compacted and its log emptied, and no term of a deleted row is on "
        "its index pages"
    )


def _index_checked(r: Pruned) -> bool:
    """Whether ``r`` scrubbed after an un-index with no text (a glob, an exclusion: no redaction), so
    its search index was to be checked in place of the text."""
    return r.redaction is None and (r.scrubbed is not None or r.scrub_error is not None)


def _left_line(left: FileCheck) -> str:
    """What the store file and its log still hold of the text: counts and places, never the text."""
    if left.error:
        return f"not checked: {left.error}"
    name = Path(left.path).name
    parts = []
    if left.occurrences or left.occurrences_any_case:
        parts.append(f"{left.occurrences:,} in {name} ({left.occurrences_any_case:,} in any case)")
    if left.wal_occurrences or left.wal_occurrences_any_case:
        parts.append(
            f"{left.wal_occurrences or 0:,} in {name}-wal "
            f"({left.wal_occurrences_any_case or 0:,} in any case)"
        )
    if left.turns:
        parts.append(_plural(left.turns, "turn"))
    if left.beliefs:
        parts.append(_plural(left.beliefs, "belief"))
    other_case = (left.turns_any_case or 0) - (left.turns or 0)
    if other_case:
        parts.append(_plural(other_case, "turn") + " in another case")
    other_case = (left.beliefs_any_case or 0) - (left.beliefs or 0)
    if other_case:
        parts.append(_plural(other_case, "belief") + " in another case")
    live = max(left.live_term_rows.values(), default=0)
    if live and not left.leftover:
        parts.append(f"its search term, indexed for {_plural(live, 'live row')}")
    parts += [f"{n:,} {where}" for where, n in left.other_rows.items()]
    deleted = sum(left.deleted_tokens.values())
    if deleted:
        parts.append(_plural(deleted, "search term") + " of deleted rows")
    return ", ".join(parts) if parts else "nothing: its bytes, rows and search terms were checked"


def _finish_command(a: argparse.Namespace) -> str:
    return f"memware --db {shlex.quote(str(a.db))} prune --scrub"


def _scrub_notes(a: argparse.Namespace, r: Pruned, dest: str | None) -> tuple[list[str], bool]:
    """What an applied prune leaves holding the text, and whether that is a failure. The scrub
    failing, or copies of the text no row accounts for, fail it: the removal did not reach the
    file. Belief rows, turns a prefix keeps, backups and transcripts are reported and do not."""
    notes, failed = [], False
    left = r.left
    close = "close other memware and Claude Code sessions, which can hold the store open, then run"
    if r.scrub_error:
        failed = True
        notes.append(
            f"the scrub did not finish ({r.scrub_error}). The removal is committed, so the text is "
            "out of every query, but the store file may still hold it. To finish, "
            f"{close}: {_finish_command(a)}"
        )
    elif r.scrubbed is not None and not r.scrubbed.wal_truncated:
        failed = True  # rewritten pages still wait in the log: the file may hold old ones
        found = f" It holds: {_left_line(left)}." if left is not None else ""
        notes.append(
            "another process was reading the store, so the scrub could not empty the write-ahead "
            "log, and the rewritten pages are still waiting in it; until they reach the file, the "
            f"file may hold removed text.{found} To finish, {close}: {_finish_command(a)}"
        )
    elif left is not None and left.leftover:
        failed = True
        notes.append(
            f"the store file still holds copies of the text no row accounts for "
            f"({_left_line(left)}). To finish, {close}: {_finish_command(a)}"
        )
    elif _index_checked(r) and (r.index_left is None or any(r.index_left.values())):
        failed = True
        found = (
            "could not be read to check it"
            if r.index_left is None
            else f"still holds {_index_found(r.index_left)}, copies of removed text no row "
            "accounts for"
        )
        notes.append(f"the search index {found}. To finish, {close}: {_finish_command(a)}")
    if left is not None and left.beliefs:
        notes.append(
            f"{_plural(left.beliefs, 'belief', 'hold')} the text past the start of a field: "
            "--turns-starting-with redacts only a leading match, and --turns-containing redacts "
            "it anywhere"
        )
    if left is not None and left.other_rows:
        where = ", ".join(f"{n:,} {w}" for w, n in left.other_rows.items())
        notes.append(f"other rows still hold the text: {where}")
    if left is not None and not left.leftover and not left.rows:
        other = (left.turns_any_case or 0) + (left.beliefs_any_case or 0)
        held = max(left.live_term_rows.values(), default=0)
        if other:
            notes.append(
                f"{_plural(other, 'turn or belief', 'hold')} the text in another case. Text is "
                "matched case-sensitively, so they are kept, and the search index keeps their term; "
                "that is not a copy of what was removed"
            )
        elif held:
            notes.append(
                f"the search index keeps the text's term for {_plural(held, 'live row')} that share "
                "it; that is not a copy of what was removed"
            )
    if r.reasons_redacted:
        notes.append(
            f"{_plural(r.reasons_redacted, 'retraction reason')} quoted the text, as a prune in "
            "memware 0.6.1 and earlier recorded it, and now read (value withheld)"
        )
    if r.scrubbed is None and r.scrub_error is None:
        return notes, failed  # nothing was removed and nothing is left: no copy to point at
    where = (
        "backups made before now may still hold the removed text: the snapshots and mirrored "
        f"transcripts in {dest}, which memware never changes"
        if dest
        else "no backup destination is configured, but a copy of the store made elsewhere may "
        "still hold the removed text"
    )
    notes.append(
        f"{where}. The transcript files are unchanged too. `memware scan"
        f"{' --backups' if dest else ''}` counts every place the value is left"
    )
    return notes, failed


_TEXT_FLAGS = ("containing", "turns_containing", "turns_starting_with")


def _prune_texts(a: argparse.Namespace) -> None:
    """Resolve the text selectors given without text, in place. ``--value-file`` serves exactly
    one of them."""
    asked = [f for f in _TEXT_FLAGS if getattr(a, f) in (_ASK, "-")]
    if getattr(a, "value_file", None) and len([f for f in asked if getattr(a, f) == _ASK]) != 1:
        raise _NoText("--value-file gives the text for one selector written without its text")
    for flag in asked:
        setattr(a, flag, _read_text(a, getattr(a, flag), "text to remove"))
        if not getattr(a, flag):
            raise _NoText(
                f"--{flag.replace('_', '-')} needs a text: an empty one matches every turn"
            )


def _cmd_scrub(a: argparse.Namespace) -> int:
    """``prune --scrub``: rewrite the store file and remove nothing."""
    if a.glob or any(getattr(a, f) is not None for f in _TEXT_FLAGS):
        print("--scrub removes nothing and takes no selector", file=sys.stderr)
        return 2
    if not Path(a.db).expanduser().exists():
        print(f"no store at {a.db}", file=sys.stderr)
        return 2
    with Store(a.db) as s:
        try:
            done: Scrubbed | None = s.scrub(
                lambda step: print(f"scrubbing the store file: {step}", file=sys.stderr)
            )
            error = None
        except (sqlite3.Error, OSError) as e:
            done, error = None, f"{type(e).__name__}: {e}"
    r = Pruned({}, 0, Retraction([], [], [], [], []), True, scrubbed=done, scrub_error=error)
    failed = bool(error) or (done is not None and not done.wal_truncated)
    if a.json:
        _out({"store_scrubbed": asdict(done) if done else None, "scrub_error": error}, True)
    else:
        print(f"store file : {_scrubbed_line(r)}")
    if failed:
        print(
            "the scrub did not finish: close other memware and Claude Code sessions, which can "
            f"hold the store open, then run it again: {_finish_command(a)}",
            file=sys.stderr,
        )
    return 1 if failed else 0


def _withheld_forms(text: str) -> list[str]:
    """Every form of a prune's text that output could carry, longest first: as given, lowercased
    (a belief's key), and escaped inside a JSON string."""
    forms = [text, text.lower(), json.dumps(text)[1:-1], json.dumps(text, ensure_ascii=False)[1:-1]]
    return sorted({f for f in forms if f}, key=len, reverse=True)


def _apply_forms(text: str, forms: list[str]) -> str:
    """``text`` with every form replaced by ``[removed]``, once each. Never call this twice on the
    same text with overlapping forms: a form that is a substring of ``removed`` itself (true of
    any one- to three-letter text) would eat into the marker a first pass already inserted."""
    for form in forms:
        text = text.replace(form, REDACTED)
    return text


def _withhold(value: Any, forms: list[str]) -> Any:
    """``value`` with every form replaced by ``[removed]``, in strings and, recursively, in the
    values of lists and dicts. Dict keys are left alone: they are memware's names, not the text."""
    if isinstance(value, str):
        return _apply_forms(value, forms)
    if isinstance(value, dict):
        return {k: _withhold(v, forms) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_withhold(v, forms) for v in value]
    return value


def _withhold_text(data: str, forms: list[str], protect: tuple[str, ...] = ()) -> str:
    """``data`` with every form replaced by ``[removed]``, except inside an occurrence of one of
    ``protect``: memware's own fixed labels and the marker itself, which a one- to three-letter
    form would otherwise match letter by letter (:func:`_apply_forms`'s note)."""
    if not protect:
        return _apply_forms(data, forms)
    pattern = "|".join(re.escape(p) for p in sorted(set(protect), key=len, reverse=True))
    parts = re.split(f"({pattern})", data)
    protected = set(protect)
    return "".join(part if part in protected else _apply_forms(part, forms) for part in parts)


class _Withheld(io.TextIOBase):
    """A text stream that passes everything through with every form of a prune's text replaced by
    ``[removed]`` (:func:`_withheld_forms`), except inside the marker itself or one of ``protect``
    -- memware's own fixed labels, never a value from the store."""

    def __init__(self, stream: Any, text: str, protect: tuple[str, ...] = ()) -> None:
        self._stream = stream
        self._forms = _withheld_forms(text)
        self._protect = (REDACTED, *protect)

    def write(self, data: str) -> int:
        self._stream.write(_withhold_text(data, self._forms, self._protect))
        return len(data)

    def flush(self) -> None:
        # also called when the wrapper is collected, by which time the wrapped stream may be closed
        with contextlib.suppress(ValueError):
            self._stream.flush()

    def isatty(self) -> bool:
        return bool(self._stream.isatty())


@contextlib.contextmanager
def _withholding(text: str | None, *, stdout: bool = True) -> Iterator[None]:
    """While a prune prints, no form of its text reaches standard error, or standard output unless
    ``stdout`` is off, by any path: counts, belief records, notes, errors. The records are withheld
    where they are built too (:func:`_withheld_plan`); this is what holds when a path is missed.
    ``--json`` withholds in the data instead, where a replacement cannot break the JSON. Labels and
    the marker (:data:`_PRUNE_LABELS`) are left alone, so a text of a letter or two stays legible
    in the counts and record fields around it."""
    if not text:
        yield
        return
    out, err = sys.stdout, sys.stderr
    sys.stderr = _Withheld(err, text, _PRUNE_LABELS)
    if stdout:
        sys.stdout = _Withheld(out, text, _PRUNE_LABELS)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = out, err


_KEY_GUARD = "\x00"


def _withheld_key(subject: str, relation: str, forms: list[str]) -> str:
    """``make_key`` from an already-withheld subject/relation, printed the way a prune shows it.
    ``make_key`` normalizes by stripping leading/trailing punctuation (:func:`memware.ledger.
    normalize`), which otherwise eats the opening ``[`` of a ``[removed]`` marker sitting at
    either edge of the field, printing ``removed] ...`` instead. The marker is swapped for a
    guard character that normalize's edge-stripping does not match, so it survives, then swapped
    back after the same case-variant safety net (:func:`_withhold`) the caller already relied on."""

    def protect(text: str) -> str:
        return text.replace(REDACTED, _KEY_GUARD)

    key = str(_withhold(make_key(protect(subject), protect(relation)), forms))
    return key.replace(_KEY_GUARD, REDACTED)


def _withheld_plan(plan: Retraction, text: str | None) -> Retraction:
    """A retraction plan as a prune prints it: every value of every belief with the text withheld
    (:func:`_withhold`), and the key made again from what is left. The plan lists beliefs as they
    were before the redaction, which would otherwise print the value the prune removes."""
    if not text:
        return plan
    forms = _withheld_forms(text)

    def row(r: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = _withhold(r, forms)
        if "key" in out and (out.get("subject"), out.get("relation")) != (
            r.get("subject"),
            r.get("relation"),
        ):
            out["key"] = _withheld_key(str(out["subject"]), str(out["relation"]), forms)
        return out

    return Retraction(
        _withhold(plan.sessions, forms),
        [row(r) for r in plan.retract],
        [row(r) for r in plan.reopen],
        [row(r) for r in plan.relink],
        [row(r) for r in plan.kept],
    )


def cmd_prune(a: argparse.Namespace) -> int:
    if a.scrub:
        return _cmd_scrub(a)
    try:
        _prune_texts(a)
    except _NoText as e:
        print(e, file=sys.stderr)
        return 2
    with _withholding(
        a.containing or a.turns_containing or a.turns_starting_with, stdout=not a.json
    ):
        return _prune(a)


def _prune(a: argparse.Namespace) -> int:
    turn_flags = [f for f in ("turns_containing", "turns_starting_with") if getattr(a, f)]
    if not (a.glob or a.containing or turn_flags):
        print(
            "prune needs --glob, --containing, --turns-containing or --turns-starting-with; "
            "with none it would un-index every source (--scrub alone rewrites the file)",
            file=sys.stderr,
        )
        return 2
    if turn_flags and (len(turn_flags) > 1 or a.glob or a.containing):
        print(
            "--turns-containing and --turns-starting-with select turns across the whole store "
            "and take no other selector",
            file=sys.stderr,
        )
        return 2
    # recorded in each retraction's reason: a glob as given, a text never
    selector = " ".join(
        f"--{flag.replace('_', '-')} " + (repr(getattr(a, flag)) if flag == "glob" else WITHHELD)
        for flag in ("glob", "containing", "turns_containing", "turns_starting_with")
        if getattr(a, flag)
    )
    refused: RedactionRefused | None = None
    with Store(a.db) as s:
        selected = {
            "glob": a.glob,
            "containing": a.containing,
            "turns_containing": a.turns_containing,
            "turns_starting_with": a.turns_starting_with,
        }
        try:
            r = prune(
                s,
                **selected,
                apply=a.apply,
                reason=f"memware prune {selector}",
                progress=lambda step: print(f"scrubbing the store file: {step}", file=sys.stderr),
                allow_broad_redaction=a.allow_broad_redaction,
            )
        except RedactionRefused as e:
            refused = e
            r = prune(s, **selected)  # the dry run it refused, for its counts
        text = a.turns_containing or a.turns_starting_with
        notes = (
            _turn_notes(s, text, prefix=bool(a.turns_starting_with), removed=r.turns)
            if text
            else _source_notes(s, a, r)
        )
    head: dict[str, Any] = {"turns_removed": r.turns}
    if not turn_flags:
        head = {"sources_pruned": len(r.sources), **head}
    lines = []
    if r.redaction is not None:
        head["beliefs_redacted"] = r.redaction.beliefs
        head["beliefs_retracted_by_redaction"] = r.redaction.retracted
        head["confirmation_sources_redacted"] = r.redaction.confirmations
        head["reviews_closed_by_redaction"] = r.redaction.reviews
        head["redaction_refused"] = r.redaction_refusal
        lines.append(
            (
                "beliefs redacted" if r.applied else "beliefs to redact",
                _redaction_line(r.redaction, r.applied),
            )
        )
        if r.redaction_refusal:
            guard = (
                f"overridden with --allow-broad-redaction: {r.redaction_refusal}"
                if r.applied
                else f"--apply refuses, because {r.redaction_refusal}; "
                "--allow-broad-redaction applies it anyway"
            )
            lines.append(("redaction guard", guard))
    failed = False
    if r.applied:
        from memware.config import get_dotted, load_config

        dest = get_dotted(load_config(), "backup.dest")
        head["retraction_reasons_redacted"] = r.reasons_redacted
        head["store_scrubbed"] = asdict(r.scrubbed) if r.scrubbed else None
        head["scrub_error"] = r.scrub_error
        head["left_in_store"] = (
            {**asdict(r.left), "leftover": r.left.leftover} if r.left is not None else None
        )
        head["left_in_index"] = r.index_left
        head["index_check"] = _index_left_line(r) if _index_checked(r) else None
        head["backup_dest"] = dest
        lines.append(("store file", _scrubbed_line(r)))
        if r.left is not None:
            lines.append(("text left in the store", _left_line(r.left)))
        if _index_checked(r):
            lines.append(("left in the search index", _index_left_line(r)))
        more, failed = _scrub_notes(a, r, dest)
        notes += more
    unwritten = "dry run: nothing written; add --apply to write it"
    if refused is not None:
        head["refused"] = str(refused)
        unwritten = (
            f"refused, nothing written: {refused}. A text that broad is more likely a word than a "
            "secret; the counts below are what --apply would do, and --allow-broad-redaction "
            "applies it anyway"
        )
    text_given = a.containing or a.turns_containing or a.turns_starting_with
    if text_given:  # --json writes around the stream guard, so its values are withheld here
        head = _withhold(head, _withheld_forms(text_given))
    _cascade(a, _withheld_plan(r.beliefs, text_given), r.applied, head, lines, unwritten=unwritten)
    if notes:
        sys.stdout.flush()  # the notes explain the counts, so they follow them in a merged stream
        print("\n".join(notes), file=sys.stderr)
    return 2 if refused is not None else 1 if failed else 0


def register(add: AddCommand) -> None:
    s = add(
        "prune",
        "un-index whole sources (--glob/--containing) or individual turns "
        "(--turns-containing/--turns-starting-with), retract beliefs from the sessions left "
        "with no turn, and scrub what was removed from the store file",
        epilog=(
            "Examples:\n"
            '  memware prune --containing "[memware-eval]"          dry run: what would go\n'
            '  memware prune --containing "[memware-eval]" --apply  un-index and retract\n'
            "  memware prune --turns-containing --apply        a pasted secret: prompts for it, unshown\n"
            "  memware prune --turns-containing --value-file F --apply   or reads it from a file\n"
            "  memware prune --scrub                           rewrite the store file, remove nothing\n"
            "Every TEXT is matched literally and case-sensitively, and never printed or recorded.\n"
            "A TEXT left out comes from --value-file, a prompt that does not echo, or stdin; `-`\n"
            "reads stdin. Run a removal from a plain terminal, not inside a Claude Code session:\n"
            "the command line lands in that session's transcript. Without --apply nothing is\n"
            "written. A belief holding the TEXT has it replaced with [removed] and keeps its row.\n"
            "Exit: 0 done · 1 the store file still\n"
            "holds removed text (the scrub did not finish) · 2 bad usage"
        ),
    )
    s.add_argument("--glob", metavar="GLOB", help="un-index sources whose path matches this glob")
    s.add_argument(
        "--containing",
        nargs="?",
        const=_ASK,
        metavar="TEXT",
        help="un-index whole sources whose transcript file contains TEXT (read whole)",
    )
    s.add_argument(
        "--turns-containing",
        nargs="?",
        const=_ASK,
        metavar="TEXT",
        help="delete individual turns holding TEXT anywhere (keeps the rest of each session)",
    )
    s.add_argument(
        "--turns-starting-with",
        nargs="?",
        const=_ASK,
        metavar="TEXT",
        help="delete individual turns that begin with TEXT, such as a recurring harness preamble",
    )
    s.add_argument(
        "--value-file",
        metavar="FILE",
        help="read the TEXT of the one text selector written without it from FILE",
    )
    s.add_argument(
        "--apply",
        action="store_true",
        help="delete the turns, retract the beliefs and scrub the file; without it nothing is written",
    )
    s.add_argument(
        "--allow-broad-redaction",
        action="store_true",
        help=f"apply even when the text would redact more than {REDACT_MAX_BELIEFS} beliefs, or "
        f"any belief for a text shorter than {REDACT_MIN_CHARS} characters",
    )
    s.add_argument(
        "--scrub",
        action="store_true",
        help="takes no selector: rewrite the store file (merge the search indexes, VACUUM, empty "
        "the write-ahead log) and remove nothing",
    )
    s.set_defaults(fn=cmd_prune)
