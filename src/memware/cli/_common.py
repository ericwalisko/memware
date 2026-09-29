"""Plumbing shared by the command modules: output, hook input, and the text helpers more than one command uses."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Protocol

from memware.digest import injection_gate, resolve_project
from memware.ingest import capture_disabled, record_no_capture
from memware.ledger import current
from memware.store import SHORT_WAIT_MS, Store
from memware.volatile import Gate


class AddCommand(Protocol):
    """What ``build_parser`` hands each command module's ``register``: it creates the
    subparser for one command, with the global flags every command accepts."""

    def __call__(
        self, name: str, help: str, epilog: str | None = None
    ) -> argparse.ArgumentParser: ...


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Show option defaults, and preserve the layout of examples in descriptions/epilogs."""


def _emit(a: argparse.Namespace, rows: object, columns: list[tuple[str, str]]) -> None:
    """Emit records honouring --json / --plain / the default view.

    --json  : indented JSON, the canonical shape for scripts that parse structure.
    --plain : one record per line, tab-separated in ``columns`` order — pipe to fzf/awk/cut.
              Tabs and newlines inside a value become spaces so each record stays on one line.
    default : labeled, one field per line, a blank line between records — linear and
              unambiguous for a screen reader, nothing aligned by eye, and no colour ever.
    """
    if getattr(a, "json", False):
        print(json.dumps(rows, indent=2, default=str))
        return
    items: list[Any] = list(rows) if isinstance(rows, list) else [rows]
    if getattr(a, "plain", False):
        for row in items:
            if isinstance(row, str):
                print(row)
                continue
            d = dict(row)
            cells = [
                "" if d.get(k) is None else str(d.get(k)).replace("\t", " ").replace("\n", " ")
                for k, _ in columns
            ]
            print("\t".join(cells))
        return
    width = max((len(lbl) for _, lbl in columns), default=0)
    for i, row in enumerate(items):
        if isinstance(row, str):
            print(row)
            continue
        if i:
            print()
        d = dict(row)
        for k, lbl in columns:
            v = d.get(k)
            if v is None or v == "":
                continue
            print(f"{lbl.rjust(width)} : " + str(v).replace(chr(10), " "))
    if not items:
        print("(no matches)")


def _hook_payload() -> dict[str, object]:
    """The harness's JSON payload on stdin, or {} when there is none or it will not parse.

    Every hook command reads its payload here, so this is also where a session run under
    MEMWARE_NO_CAPTURE puts its transcript on the no-capture list. The catch-up sync and the
    backup mirror run without the variable, and the list is how they learn to leave the
    transcript out. Recording prints nothing, and a failure to record never fails the hook."""
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    transcript = payload.get("transcript_path")
    if transcript and capture_disabled():
        with contextlib.suppress(Exception):
            record_no_capture(str(transcript))
    return payload


def _out(obj: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    elif isinstance(obj, list):
        for row in obj:
            print(row if isinstance(row, str) else json.dumps(row, default=str))
    else:
        print(obj if isinstance(obj, str) else json.dumps(obj, default=str))


EXCLUDE_SHARE_WARN = 0.5


def _transcripts_on_disk(src: str) -> list[str]:
    """Resolved paths of the transcripts under ``src``: the files the catch-up sync and the backup
    mirror walk, as ``capture.exclude`` patterns see them."""
    root = Path(src).expanduser()
    if not root.is_dir():
        return []
    return sorted(str(p.resolve()) for p in root.rglob("*.jsonl") if p.is_file())


def _of(part: int, whole: int) -> str:
    return f"{part:,} of {whole:,}" + (f" ({part / whole:.1%})" if whole else "")


def _hiding_verdict(excluded: int, total: int, *, pointer: bool = True) -> str | None:
    """A line when the exclusions hide a large share of the transcripts, else None."""
    if not total or excluded / total < EXCLUDE_SHARE_WARN:
        return None
    line = f"capture.exclude hides {_of(excluded, total)} transcripts on disk"
    return line + ("; `memware exclude` lists what each pattern matches" if pointer else "")


def _print_blocks(blocks: list[list[tuple[str, str]]]) -> None:
    """Labeled ``field : value`` lines, a blank line between blocks, as ``memware stats`` prints."""
    blocks = [b for b in blocks if b]
    width = max((len(label) for b in blocks for label, _ in b), default=0)
    print(
        "\n\n".join(
            "\n".join(f"{label.rjust(width)} : {value}" for label, value in b) for b in blocks
        )
    )


def _hook_store(db: str) -> Store | None:
    """The store for a foreground hook, which Claude Code gives 5 or 10 seconds. An open waits for
    the write lock only when an upgrade adds a table, and a hook waits :data:`SHORT_WAIT_MS` for it.
    When another writer holds the lock past that, it returns None: the hook has nothing to say this
    time, and the next open, a sync's or a command's, creates the table."""
    try:
        return Store(db, busy_timeout_ms=SHORT_WAIT_MS)
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            return None
        raise


def _gate(a: argparse.Namespace) -> Gate:
    return injection_gate(resolve_project(Path(a.cwd or os.getcwd()).expanduser()))


def _stale(s: Store, gate: Gate, subject: str | None = None) -> list[dict[str, Any]]:
    """Current beliefs the injection gate leaves out, each with its reason and why."""
    out = []
    for row in current(s, subject):
        v = gate.verdict(row)
        if v is not None:
            out.append({**row, "reason": v.reason, "why": v.detail})
    return out


def _n(value: int | str) -> str:
    """A count with thousands separators; text as it is."""
    return f"{value:,}" if isinstance(value, int) else value


def _plural(n: int, noun: str, verb: str = "") -> str:
    """``1 turn contains``, ``2 turns contain``: a count, its noun and, if given, the verb."""
    phrase = f"{n:,} {noun}{'' if n == 1 else 's'}"
    return f"{phrase} {verb}{'s' if n == 1 else ''}" if verb else phrase


_ASK = "\0ask"


class _NoText(Exception):
    """A text option could not be read; the message says why, never what."""


def _read_text(a: argparse.Namespace, given: str | None, what: str) -> str:
    """The text for an option or argument: as given, from ``--value-file`` when it was left out,
    from a prompt that does not echo when standard input is a terminal, or else one line of
    standard input. ``-`` also reads standard input. See :func:`_one_line` for what is refused."""
    import getpass

    if given not in (None, _ASK, "-"):
        return _one_line(str(given))
    if given != "-" and getattr(a, "value_file", None):
        try:
            text = Path(a.value_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise _NoText(f"--value-file could not be read: {e}") from e
        return _one_line(text)
    if given != "-" and sys.stdin.isatty():
        return _one_line(getpass.getpass(f"{what} (not shown): ", stream=sys.stderr))
    return _one_line(sys.stdin.readline())


def _one_line(text: str) -> str:
    """The text with every trailing line break dropped. One that still holds a line break is
    refused: a file with a stray blank line would otherwise search for the value plus a newline,
    match nothing, and report a clean store while a transcript still holds the value."""
    text = text.rstrip("\r\n")
    if "\n" in text or "\r" in text:
        raise _NoText(
            "the value holds a line break, so it would match nothing a transcript holds and "
            "report it clean; give it as one line (a --value-file must not start with a blank line)"
        )
    return text


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"
