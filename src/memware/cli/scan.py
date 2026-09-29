"""`memware scan`: where a value is still stored."""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict

from memware.cli._common import AddCommand, _n, _NoText, _out, _plural, _print_blocks, _read_text
from memware.residue import FileCheck
from memware.scan import ScanReport, TranscriptHit, scan


def _indexed_line(hit: TranscriptHit) -> str:
    if hit.indexed and hit.excluded_by:
        return f"yes, and excluded by {hit.excluded_by}: the next sync un-indexes it"
    if hit.indexed:
        return "yes"
    return f"no: excluded by {hit.excluded_by}" if hit.excluded_by else "no: not synced yet"


def _either_case(n: int, folded: int | None) -> str:
    return f"{n:,} ({folded or 0:,} in any case)"


def _db_block(label: str, c: FileCheck) -> list[tuple[str, str]]:
    """One SQLite file's counts. Never the text: only how often and where."""
    rows = [(label, c.path), ("occurrences", _either_case(c.occurrences, c.occurrences_any_case))]
    if c.wal_occurrences is not None:
        rows.append(
            ("-wal occurrences", _either_case(c.wal_occurrences, c.wal_occurrences_any_case))
        )
    if c.error:
        rows.append(("not queried", c.error))
        return rows
    held = ", ".join(f"{t} {n:,} of {c.tokens:,}" for t, n in c.index_tokens.items())
    deleted = sum(c.deleted_tokens.values())
    rows += [
        ("turns holding it", _n(c.turns or 0)),
        ("beliefs holding it", _n(c.beliefs or 0)),
        *[(f"{where} holding it", _n(n)) for where, n in c.other_rows.items()],
        ("search terms held", held if c.tokens else "the value makes no search term"),
    ]
    if deleted:
        rows.append(("of deleted rows", f"{deleted:,}: `memware prune --scrub` removes them"))
    rows.append(("free pages", _n(c.free_pages or 0)))
    return rows


def _scan_verdict(r: ScanReport) -> str:
    places = []
    if r.transcripts:
        places.append(_plural(len(r.transcripts), "transcript"))
    if r.store_check and r.store_check.found:
        places.append("the store file")
    copies = sum(c.found for c in r.store_copies)
    if copies:
        places.append(_plural(copies, "pre-restore copy"))
    if r.mirrored:
        places.append(_plural(len(r.mirrored), "mirrored transcript"))
    snaps = sum(c.found for c in r.snapshots)
    if snaps:
        places.append(_plural(snaps, "snapshot"))
    if places:
        return "found in " + ", ".join(places)
    if r.unread:
        return f"not found, but {_plural(len(r.unread), 'path')} could not be read: see below"
    return "not found"


def cmd_scan(a: argparse.Namespace) -> int:
    """Where a value is still stored. Exit 0 when nothing is found and everything was read, 1 when
    anything is found, 2 when nothing is found but something could not be read (or on bad usage).
    Paths and counts only: the value is never printed, nor any text around it."""
    from memware.config import get_dotted, load_config

    try:
        value = _read_text(a, a.value, "text to scan for")
    except _NoText as e:
        print(e, file=sys.stderr)
        return 2
    if not value:
        print("scan needs a value: an empty one would match everything", file=sys.stderr)
        return 2
    cfg = load_config()
    src = a.transcript_src or get_dotted(cfg, "backup.transcript_src") or "~/.claude/projects"
    dest = None
    if a.backups or a.dest:
        dest = a.dest or get_dotted(cfg, "backup.dest")
        if not dest:
            print(
                "--backups needs a destination: pass --dest DIR or set backup.dest "
                "(`memware setup`)",
                file=sys.stderr,
            )
            return 2
    r = scan(value, db=a.db, transcript_src=src, backup_dest=dest)
    code = 1 if r.found else 0 if r.complete else 2
    if a.json:
        body = asdict(r)
        if r.store_check:
            body["store_check"]["found"] = r.store_check.found
        for key, checks in (("store_copies", r.store_copies), ("snapshots", r.snapshots)):
            for row, check in zip(body[key], checks, strict=True):
                row["found"] = check.found
        _out({"found": r.found, "complete": r.complete, **body}, True)
        return code
    blocks: list[list[tuple[str, str]]] = [
        [
            ("verdict", _scan_verdict(r)),
            (
                "transcript source",
                f"{r.transcript_src} ({_plural(r.transcripts_read, 'transcript')} read)",
            ),
        ]
    ]
    for hit in r.transcripts:
        blocks.append(
            [
                ("transcript", hit.path),
                ("occurrences", _n(hit.occurrences)),
                ("indexed", _indexed_line(hit)),
            ]
        )
    if r.indexed_gone:
        blocks.append(
            [
                (
                    "indexed, file gone",
                    f"{_plural(len(r.indexed_gone), 'source')}: their turns are counted in the "
                    "store below",
                )
            ]
        )
    if r.store_check:
        blocks.append(_db_block("store", r.store_check))
    else:
        state = "could not be read: see below" if r.store_exists else "no store file"
        blocks.append([("store", f"{r.store} ({state})")])
    blocks += [_db_block("pre-restore copy", c) for c in r.store_copies]
    if r.backup_dest is not None:
        blocks.append(
            [
                (
                    "backup destination",
                    f"{r.backup_dest} ({_plural(r.mirrored_read, 'mirrored transcript')} read, "
                    f"{_plural(len(r.snapshots), 'snapshot')})",
                )
            ]
        )
        blocks += [
            [("mirrored transcript", hit.path), ("occurrences", _n(hit.occurrences))]
            for hit in r.mirrored
        ]
        blocks += [_db_block("snapshot", c) for c in r.snapshots]
    blocks += [[("not read", u.path), ("reason", u.reason)] for u in r.unread]
    _print_blocks(blocks)
    return code


def register(add: AddCommand) -> None:
    s = add(
        "scan",
        "count where a value is still stored: transcripts on disk, whether each is indexed, the "
        "store file and its search index, and with --backups the backup destination (read-only)",
        epilog=(
            "Examples:\n"
            "  memware scan --backups                prompts for the value, unshown; then every\n"
            "                                        transcript, the store file, its index, backups\n"
            "  memware scan --value-file F --json    the value from a file\n"
            "  pbpaste | memware scan - --json       the value from stdin\n"
            "Prints paths and counts, never the value or text around it. Transcripts match\n"
            "literally and case-sensitively. Run it from a plain terminal: a value on the command\n"
            "line is visible in ps and shell history, and inside a Claude Code session it lands in\n"
            "that session's transcript. Exit: 0 not found · 1 found · 2 not found, but a path could\n"
            "not be read. See docs/keeping-memory-clean.md."
        ),
    )
    s.add_argument(
        "value",
        nargs="?",
        metavar="VALUE",
        help="the text to look for; left out, --value-file, a prompt that does not echo, or "
        "stdin gives it, and `-` reads stdin",
    )
    s.add_argument("--value-file", metavar="FILE", help="read VALUE from FILE")
    s.add_argument(
        "--backups",
        action="store_true",
        help="also read the backup destination: mirrored transcripts and snapshot files",
    )
    s.add_argument(
        "--dest", metavar="DIR", help="backup destination (implies --backups; default: backup.dest)"
    )
    s.add_argument(
        "--transcript-src",
        metavar="DIR",
        help="transcript tree to walk (default: backup.transcript_src)",
    )
    s.set_defaults(fn=cmd_scan)
