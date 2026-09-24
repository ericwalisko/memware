"""``memware`` command-line interface. Every command is also usable from a hook:
pass ``--from-hook`` to read the harness's JSON payload on stdin."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import sqlite3
import sys
from collections.abc import Collection, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from memware import __version__, relevance
from memware.derive import add_arguments as _derive_arguments
from memware.derive import cmd_derive, open_readonly
from memware.derive import status as derive_status
from memware.digest import (
    CONTEXT_TITLE,
    DEFAULT_MAX_CHARS,
    belief_line,
    digest,
    injection_gate,
    project_dir_name,
    resolve_project,
)
from memware.index import (
    read_turns,
    search_beliefs,
    search_beliefs_multi,
    search_turns_multi,
)
from memware.ingest import (
    WITHHELD,
    Pruned,
    capture_disabled,
    prune,
    record_no_capture,
    sync_file,
    sync_tree,
    turn_matches,
)
from memware.ledger import (
    REDACT_MAX_BELIEFS,
    REDACT_MIN_CHARS,
    REDACTED,
    Policy,
    Redaction,
    RedactionRefused,
    Retraction,
    approve,
    assert_belief,
    confirmed_sql,
    current,
    history,
    make_key,
    orphaned_count,
    reject,
    retract,
    stale_turn_count,
)
from memware.residue import FileCheck
from memware.review import HttpReviewBackend, JsonlReviewBackend, open_reviews, sync_reviews
from memware.scan import ScanReport, TranscriptHit, scan
from memware.store import HOOK_SYNC_WAIT_MS, SHORT_WAIT_MS, Scrubbed, Store, now_iso
from memware.volatile import REASONS, WINDOW_KEY, Gate, label, older_version, parse_days


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Show option defaults, and preserve the layout of examples in descriptions/epilogs."""


# (key, label) column orders for the record-listing commands. Used for --plain (tab-separated,
# in this order) and for the default labeled view; keys absent from a row are skipped.
_RECALL_COLS = [
    ("id", "id"),
    ("kind", "kind"),
    ("score", "score"),
    ("session", "session"),
    ("ts", "when"),
    ("role", "role"),
    ("subject", "subject"),
    ("relation", "relation"),
    ("source", "source"),
    ("text", "text"),
    ("volatile", "volatile"),  # last: --plain column positions stay where scripts expect them
]
_BELIEF_COLS = [
    ("id", "id"),
    ("subject", "subject"),
    ("relation", "relation"),
    ("value", "value"),
    ("valid_from", "valid from"),
    ("valid_to", "valid to"),
    ("reliability", "reliability"),
    ("status", "status"),
    ("source", "source"),
    ("volatile", "volatile"),  # last: --plain column positions stay where scripts expect them
]
_STALE_COLS = [
    ("id", "id"),
    ("subject", "subject"),
    ("relation", "relation"),
    ("value", "value"),
    ("valid_from", "valid from"),
    ("reason", "left out"),
    ("why", "why"),
    ("source", "source"),
]
_TURN_COLS = [("id", "id"), ("seq", "seq"), ("role", "role"), ("ts", "when"), ("text", "text")]


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


def cmd_init(a: argparse.Namespace) -> int:
    _maybe_setup_hint(a)
    with Store(a.db) as s:
        _out({"db": str(s.path), **s.stats()}, a.json)
    return 0


def cmd_sync(a: argparse.Namespace) -> int:
    paths = list(a.paths)
    if a.from_hook:
        tp = _hook_payload().get("transcript_path")  # read first: that records it if need be
        if capture_disabled():
            return 0  # MEMWARE_NO_CAPTURE=1: this run must not enter the store
        if tp:
            paths.append(str(tp))
    if not paths and not a.from_hook:
        # Bare `memware sync` = catch up the configured transcript source (default
        # ~/.claude/projects). The SessionStart hook uses this to index sessions whose
        # SessionEnd never ran — e.g. a worktree manager that SIGKILLs the process group.
        from memware.config import get_dotted, load_config

        src = get_dotted(load_config(), "backup.transcript_src")
        if src:
            paths.append(str(src))
    if not paths:
        print("nothing to sync", file=sys.stderr)
        return 0
    try:
        # A hook's sync waits a few seconds for the lock, not a minute: PreCompact runs it in the
        # foreground with a 30 s timeout, and the next sync catches up from each cursor.
        with Store(a.db, busy_timeout_ms=HOOK_SYNC_WAIT_MS if a.from_hook else None) as s:
            report: dict[str, int] = {}
            for p in paths:
                path = Path(p).expanduser()
                if path.is_dir():
                    report.update(
                        sync_tree(
                            s,
                            path,
                            harness=a.harness,
                            skip_if_contains=a.skip_if_contains,
                            exclude=a.exclude,
                        )
                    )
                elif path.exists():
                    report[str(path)] = sync_file(
                        s, path, harness=a.harness, skip_if_contains=a.skip_if_contains
                    )
            _out({"added": sum(report.values()), "files": len(report)}, a.json or a.from_hook)
    except sqlite3.OperationalError as e:
        if a.from_hook and ("locked" in str(e) or "busy" in str(e)):
            return 0  # quietly: another writer held the store, and the next sync catches up
        raise
    return 0


def _warn_if_backup_is_larger(s: Store, a: argparse.Namespace) -> None:
    """If a backup holds materially more than this store, the user may have wiped it while
    transcripts past the OS's 30-day cleanup are already gone. Backfill can only re-index
    what is on disk, so steer them to restore instead. Warning only — backfill never deletes."""
    try:
        from memware import backup as bk
        from memware.config import get_dotted, load_config

        dest = get_dotted(load_config(), "backup.dest")
        snaps = bk.list_snapshots(dest) if dest else []
        if not snaps:
            return
        here = s.stats()["turns"]
        import sqlite3

        con = sqlite3.connect(f"file:{snaps[0]}?mode=ro", uri=True)
        try:
            there = int(con.execute("SELECT count(*) FROM turn").fetchone()[0])
        finally:
            con.close()
        if there > here + 100:
            print(
                f"WARNING: backup {snaps[0].name} holds {there:,} turns; this store has {here:,}. "
                f"If you wiped the store and transcripts older than your OS's retention are gone, "
                f"backfill cannot bring them back — restore instead:\n"
                f"    memware restore --latest\n"
                f"Continuing will index only the transcripts currently on disk.",
                file=sys.stderr,
            )
    except Exception:
        pass


def cmd_backfill(a: argparse.Namespace) -> int:
    """One-time index of existing transcripts on a fresh machine.

    The plugin only captures new sessions; this reads what is already on disk.
    Idempotent — safe to re-run — and it honours the ignore-markers list.
    """
    _maybe_setup_hint(a)
    root = Path(a.root).expanduser()
    if not root.exists():
        print(f"nothing to backfill: {root} does not exist", file=sys.stderr)
        return 0
    with Store(a.db) as s:
        _warn_if_backup_is_larger(s, a)
        report = sync_tree(s, root, harness=a.harness, exclude=a.exclude)
        added = sum(report.values())
        stats = s.stats()
    _out(
        {
            "root": str(root),
            "files": len(report),
            "turns_added": added,
            "turns_total": stats["turns"],
            "sessions": stats["sessions"],
        },
        a.json,
    )
    return 0


EXCLUDE_SHARE_WARN = 0.5
"""``capture.exclude`` hiding at least this share of the transcripts on disk is called out by
``memware exclude``, ``memware stats`` and ``memware backup``."""


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


def _path_names(src: str) -> set[str]:
    """The names in a transcript source's path, as given and resolved."""
    root = Path(src).expanduser()
    return {*root.parts, *root.resolve().parts} - {"/"}


_LAYOUT_NAMES = ("subagents",)
"""Directory names Claude Code itself writes under a project directory."""


def _last_name(pattern: str, src_names: Collection[str] = ()) -> str | None:
    """The last name in a pattern written as a path that can be a project's: the last
    ``/``-separated segment with its wildcards and edge dashes taken out, skipping one left empty,
    a ``.jsonl`` file's name, a directory Claude Code writes itself (:data:`_LAYOUT_NAMES`) and a
    name in the transcript source's own path (``src_names``). ``*/privateproj*``,
    ``*/privateproj/*/subagents/*`` and ``~/.claude/projects/privateproj/*`` all give
    ``privateproj``."""
    parts = os.path.expanduser(pattern).split("/")
    for i in reversed(range(len(parts))):
        name = re.sub(r"\[[^]]*\]|[*?]", "", parts[i]).strip("-")
        if i == len(parts) - 1 and name.endswith(".jsonl"):
            continue
        if name and name not in _LAYOUT_NAMES and not {name, parts[i]} & set(src_names):
            return name
    return None


def _segment_forms(pattern: str, src_names: Collection[str] = ()) -> list[str]:
    """The forms that name the Claude Code project a pattern written as a path means, the one that
    keeps a project out first. Claude Code keeps a project's transcripts in a directory named after
    the whole path it ran in, with every character but a letter or digit made a dash
    (:func:`memware.digest.project_dir_name`), and a session started in a subdirectory or a
    worktree of the project in a directory of its own (``-Users-me-work--claude-worktrees-feat``).
    So ``*work*`` names the project with its subdirectories and worktrees, and any other directory
    whose name holds ``work``; ``*-work/*`` names only the directory ``work`` itself. Built from
    :func:`_last_name`; empty when the pattern has none."""
    name = _last_name(pattern, src_names)
    encoded = project_dir_name(name).strip("-") if name else ""
    return [f"*{encoded}*", f"*-{encoded}/*"] if encoded else []


def _segment_verdict(suggestions: list[dict[str, Any]]) -> str:
    """Why a pattern written as a path matched nothing, and the forms that match, each with what it
    matches. ``suggestions`` holds only forms that match something, the broad one first."""
    why = (
        "Claude Code keeps a project's transcripts in a directory named after the whole path it ran "
        "in, with every character but a letter or digit made a dash: /Users/me/work is "
        "-Users-me-work, and a worktree of it -Users-me-work--claude-worktrees-feat"
    )
    if not suggestions:
        return f"{why}; no form of the pattern's last name matches anything either"

    def counted(s: dict[str, Any]) -> str:
        return (
            f"{shlex.quote(s['pattern'])} ({_plural(s['transcripts'], 'transcript')} on disk, "
            f"{_plural(s['indexed_sources'], 'indexed source')})"
        )

    line = (
        f"{why}. To keep the project out, use {counted(suggestions[0])}: it covers sessions "
        "started in the project, its subdirectories and its worktrees, and in any other directory "
        "whose name holds that name"
    )
    for narrow in suggestions[1:]:
        line += (
            f". {counted(narrow)} covers only sessions started in a directory of that name itself, "
            "and leaves its subdirectories' and worktrees' sessions indexed"
        )
    return line


_SEGMENT_REFUSAL = "a pattern written as a path that matches nothing is most likely mistyped"


def _print_blocks(blocks: list[list[tuple[str, str]]]) -> None:
    """Labeled ``field : value`` lines, a blank line between blocks, as ``memware stats`` prints."""
    blocks = [b for b in blocks if b]
    width = max((len(label) for b in blocks for label, _ in b), default=0)
    print(
        "\n\n".join(
            "\n".join(f"{label.rjust(width)} : {value}" for label, value in b) for b in blocks
        )
    )


def cmd_exclude(a: argparse.Namespace) -> int:
    """List, add or remove ``capture.exclude`` patterns. A dry run unless ``--apply``.

    The dry run counts, for every pattern, the transcripts on disk it matches and the indexed
    sources it would un-index, and reads the store read-only. ``--apply`` writes the config and
    un-indexes every indexed source the patterns match, including sources whose transcript is
    no longer on disk, which no sync would visit again, then scrubs the store file as an applied
    prune does (:func:`memware.ingest.unindex_sources`). Removing a pattern un-indexes nothing
    and indexes nothing: the next sync picks up the transcripts it was hiding.

    A new pattern written as a path (holding a ``/``) that matches nothing, such as
    ``*/olivia-career/*`` where Claude Code names the directory ``-Users-me-olivia-career``, is
    refused on ``--apply`` unless ``--force``; the output names the forms that match, the one that
    keeps the project out first. A pattern already in ``capture.exclude`` adds nothing."""
    import sqlite3

    from memware.config import (
        config_path,
        get_dotted,
        load_config,
        load_user_config,
        save_config,
        set_dotted,
    )
    from memware.ingest import (
        capture_exclude_patterns,
        is_excluded,
        matches_exclude,
        unindex_sources,
    )

    before = capture_exclude_patterns()
    pattern = (a.add if a.add is not None else a.remove or "").strip()
    action = "add" if a.add is not None else "remove" if a.remove is not None else "list"
    if action != "list" and not pattern:
        print("an empty pattern matches nothing", file=sys.stderr)
        return 2
    if action == "remove" and pattern not in before:
        print(f"not in capture.exclude: {pattern}", file=sys.stderr)
        return 2
    if action == "add":
        after = before if pattern in before else [*before, pattern]
    elif action == "remove":
        after = [p for p in before if p != pattern]
    else:
        after = before

    src = str(get_dotted(load_config(), "backup.transcript_src") or "~/.claude/projects")
    disk = _transcripts_on_disk(src)
    db = Path(a.db).expanduser()
    indexed: dict[str, int] = {}  # source -> turns, for sources some pattern of interest matches
    sources: list[str] = []  # every indexed source, for what a suggested form would match
    watched = [*after, pattern] if action == "remove" else after
    if db.exists() and watched:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # a dry run never writes
        try:
            for (source,) in con.execute("SELECT source FROM cursor").fetchall():
                sources.append(source)
                if is_excluded(source, watched):
                    row = con.execute("SELECT count(*) FROM turn WHERE source=?", (source,))
                    indexed[source] = int(row.fetchone()[0])
        finally:
            con.close()

    def match(pat: str) -> dict[str, Any]:
        hits = [src_ for src_ in indexed if matches_exclude(src_, pat)]
        return {
            "pattern": pat,
            "transcripts": sum(matches_exclude(p, pat) for p in disk),
            "indexed_sources": len(hits),
            "indexed_turns": sum(indexed[h] for h in hits),
        }

    rows = [match(pat) for pat in after]
    unindex = [] if action == "remove" else [s_ for s_ in indexed if is_excluded(s_, after)]
    report: dict[str, Any] = {
        "action": action,
        "pattern": pattern or None,
        "applied": bool(a.apply),
        "config": str(config_path()),
        "transcript_src": src,
        "transcripts": len(disk),
        "patterns": rows,
        "excluded": sum(is_excluded(p, after) for p in disk),
        "unindex_sources": len(unindex),
        "unindex_turns": sum(indexed[s_] for s_ in unindex),
    }
    target = match(pattern) if pattern else None
    if action == "remove":
        report["removed"] = target
        report["reindexable"] = sum(
            matches_exclude(p, pattern) and not is_excluded(p, after) for p in disk
        )
    nothing = bool(
        action == "add" and target and not (target["transcripts"] or target["indexed_sources"])
    )
    already = action == "add" and pattern in before
    path_like = nothing and "/" in os.path.expanduser(pattern)
    counted = (
        {
            "pattern": form,
            "transcripts": sum(matches_exclude(p, form) for p in disk),
            "indexed_sources": sum(matches_exclude(s_, form) for s_ in sources),
        }
        for form in (_segment_forms(pattern, _path_names(src)) if path_like else [])
    )
    suggestions = [c for c in counted if c["transcripts"] or c["indexed_sources"]]
    refused = bool(a.apply and path_like and not already and not a.force)
    report["suggestions"] = suggestions
    report["refused"] = _SEGMENT_REFUSAL if refused else None
    report["applied"] = bool(a.apply) and not refused

    pruned: Pruned | None = None
    notes: list[str] = []
    failed = False
    if report["applied"]:
        if after != before:
            user = load_user_config()  # write the one key; defaults stay defaults
            set_dotted(user, "capture.exclude", after)
            save_config(user)
        if unindex:
            with Store(db) as s:
                pruned = unindex_sources(  # as a sync un-indexes: beliefs are left alone
                    s,
                    unindex,
                    progress=lambda step: print(
                        f"scrubbing the store file: {step}", file=sys.stderr
                    ),
                )
                report["beliefs_orphaned"] = orphaned_count(s)
            dest = get_dotted(load_config(), "backup.dest")
            report["store_scrubbed"] = asdict(pruned.scrubbed) if pruned.scrubbed else None
            report["scrub_error"] = pruned.scrub_error
            report["left_in_index"] = pruned.index_left
            report["index_check"] = _index_left_line(pruned)
            report["backup_dest"] = dest
            notes, failed = _scrub_notes(a, pruned, dest)
    code = 2 if refused else 1 if failed else 0

    if a.json:
        _out(report, True)
        if notes:
            print("\n".join(notes), file=sys.stderr)
        return code
    state = (
        "refused, nothing written"
        if refused
        else "applied"
        if report["applied"]
        else "dry run, nothing written"
    )
    blocks: list[list[tuple[str, str]]] = [
        [
            ("action", f"{action} {pattern} ({state})" if pattern else f"list ({state})"),
            ("config", str(config_path())),
            ("transcript source", f"{src} ({len(disk):,} transcripts)"),
        ]
    ]
    shown = [*rows, target] if action == "remove" and target else rows
    for r in shown:
        mark = ""
        if r["pattern"] == pattern:
            mark = " (removed)" if action == "remove" else "" if pattern in before else " (new)"
        blocks.append(
            [
                ("pattern", r["pattern"] + mark),
                ("transcripts", f"{r['transcripts']:,}"),
                ("indexed sources", f"{r['indexed_sources']:,} ({r['indexed_turns']:,} turns)"),
            ]
        )
    total = [("transcripts excluded", _of(report["excluded"], len(disk)))]
    if action == "remove":
        total.append(("transcripts no longer excluded", f"{report['reindexable']:,}"))
    else:
        label = "sources un-indexed" if report["applied"] else "sources to un-index"
        total.append((label, f"{len(unindex):,} ({report['unindex_turns']:,} turns)"))
    if pruned is not None:
        total.append(("store file", _scrubbed_line(pruned)))
        if _index_checked(pruned):
            total.append(("left in the search index", _index_left_line(pruned)))
    blocks.append(total)

    verdicts: list[str] = []
    if not after:
        verdicts.append("capture.exclude is empty; `memware exclude --add GLOB` previews a pattern")
    if already:
        verdicts.append(f"{pattern} is already in capture.exclude, so --add changes nothing")
    if nothing:
        how = (
            _segment_verdict(suggestions)
            if path_like
            else "it is matched against the whole resolved path, and `*` crosses `/`"
        )
        verdicts.append(
            "the pattern matches no transcript on disk and no indexed source, so it excludes "
            f"nothing now. {how[0].upper()}{how[1:]}"
        )
    if path_like and not already:
        force = "--force adds it anyway, for a project that has not run yet"
        verdicts.append(
            f"refused: {_SEGMENT_REFUSAL}; {force}"
            if refused
            else f"--apply refuses it, because {_SEGMENT_REFUSAL}; {force}"
            if not a.apply
            else "added with --force, though it matches nothing"
        )
    hiding = _hiding_verdict(report["excluded"], len(disk), pointer=False)
    if hiding:
        verdicts.append(hiding)
    if action == "remove" and report["reindexable"]:
        verdicts.append("the next sync indexes the transcripts it no longer excludes")
    orphaned = report.get("beliefs_orphaned")
    if orphaned:
        verdicts.append(
            f"{orphaned:,} {'belief cites' if orphaned == 1 else 'beliefs cite'} a session that is "
            "no longer indexed. `memware beliefs retract --orphaned` lists them; add --apply to "
            "retract."
        )
    verdicts += notes
    if not a.apply:
        steps = []
        if action == "add" and after != before and not path_like:
            steps.append("add it to capture.exclude")
        if action == "remove":
            steps.append("remove it from capture.exclude")
        if unindex:
            steps.append("un-index what the patterns match and scrub the store file")
        if steps:
            verdicts.append("run again with --apply to " + " and ".join(steps))
    blocks.append([("verdict", v) for v in verdicts])
    _print_blocks(blocks)
    return code


def cmd_recall(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        hits = []
        if a.what in ("all", "beliefs"):
            hits += search_beliefs_multi(s, a.queries, k=a.k, record_use=not a.no_touch)
        if a.what in ("all", "turns"):
            hits += search_turns_multi(
                s, a.queries, k=a.k, record_use=not a.no_touch, snippet_tokens=a.snippet_tokens
            )
        rows = [
            {
                "kind": h.kind,
                "id": h.id,
                "score": round(h.score, 4),
                "session": h.session,
                "ts": h.ts,
                "role": h.role,
                "subject": h.subject,
                "relation": h.relation,
                "source": h.source,
                "offset": h.offset,
                "volatile": h.volatile,
                "snippet": h.snippet,
                "text": h.text if a.full else h.text[:300],
            }
            for h in hits
        ]
        _emit(a, rows, _RECALL_COLS)
    return 0


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


def cmd_context(a: argparse.Namespace) -> int:
    """Prompt-time helper: print the beliefs whose subject the prompt names, less what the
    injection gate leaves out (memware.volatile) and, when switched on, what the relevance filter
    judges irrelevant (memware.relevance; off by default)."""
    payload = _hook_payload() if a.from_hook or not a.prompt else {}
    prompt = a.prompt or str(payload.get("prompt", ""))
    if not prompt.strip():
        return 0
    gate = injection_gate(resolve_project(Path(str(payload.get("cwd") or os.getcwd()))))
    store = _hook_store(a.db) if a.from_hook else Store(a.db)
    if store is None:
        return 0
    with store as s:
        # Injection is not retrieval: nobody asked for these, so they must not gain activation
        # or count as used — use_count means an agent or a person retrieved the belief. Ranked
        # past k, so a left-out belief makes room for the next one rather than a shorter block.
        hits = search_beliefs(s, prompt, k=100, require_subject=True, record_use=False)
        if not hits:
            return 0
        rows = {
            r["id"]: r
            for r in s.conn.execute(
                f"SELECT *, {confirmed_sql()} FROM belief WHERE id IN ({','.join('?' * len(hits))})",
                [h.id for h in hits],
            )
        }
    admitted = [r for r in (rows[h.id] for h in hits if h.id in rows) if gate.verdict(r) is None]
    rel = relevance.settings()
    if rel.on:  # opted in: the network call happens here, after the store is closed
        picked = relevance.choose(
            prompt,
            [(r["id"], relevance.fact(r["subject"], r["relation"], r["value"])) for r in admitted],
            a.k,
            rel,
            harness="claude-code" if a.from_hook else "cli",
            session=str(payload.get("session_id") or "") or None,
            transcript=str(payload.get("transcript_path") or "") or None,
            agent=bool(payload.get("agent_id")),
        )
        admitted = [admitted[i] for i in picked]
    lines = [
        belief_line(r["subject"], r["relation"], r["value"], r["valid_from"]) for r in admitted
    ][: a.k]
    if not lines:
        return 0
    block = CONTEXT_TITLE + "\n" + "\n".join(lines)
    if a.from_hook:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": block,
                    }
                }
            )
        )
    else:
        print(block)
    return 0


def cmd_digest(a: argparse.Namespace) -> int:
    """Session-start helper: what memware holds for this project (see memware.digest)."""
    payload = _hook_payload() if a.from_hook else {}
    cwd = a.cwd or payload.get("cwd") or os.getcwd()
    if a.db != ":memory:" and not Path(a.db).expanduser().exists():
        return 0  # no store yet: nothing to say, and a hook must not create one
    session, transcript = payload.get("session_id"), payload.get("transcript_path")
    store = _hook_store(a.db) if a.from_hook else Store(a.db)
    if store is None:
        return 0
    with store as s:
        block = digest(
            s,
            Path(str(cwd)).expanduser(),
            k=a.k,
            max_chars=a.max_chars,
            session=str(session) if session else None,
            transcript_path=str(transcript) if transcript else None,
        )
    if not block:
        return 0
    if a.from_hook:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": block,
                    }
                }
            )
        )
    else:
        print(block)
    return 0


def cmd_assert(a: argparse.Namespace) -> int:
    if a.subject == "-":
        return _assert_stdin(a)
    if a.relation is None or a.value is None:
        print(
            "assert needs SUBJECT RELATION VALUE, or `-` to read TSV lines from stdin",
            file=sys.stderr,
        )
        return 2
    with Store(a.db) as s:
        r = assert_belief(
            s,
            a.subject,
            a.relation,
            a.value,
            valid_from=a.valid_from,
            source=a.source,
            reliability=a.reliability,
            policy=Policy(a.policy),
        )
        _out(
            {
                "outcome": r.outcome.value,
                "belief_id": r.belief_id,
                "incumbent_id": r.incumbent_id,
                "review_id": r.review_id,
            },
            a.json,
        )
    return 0


def _assert_stdin(a: argparse.Namespace) -> int:
    """Batch-assert tab-separated ``subject<TAB>relation<TAB>value[<TAB>source]`` lines from
    stdin. Blank lines and lines starting with ``#`` are skipped. Pairs with ``beliefs --plain``
    so facts can round-trip through an editor or script: read them out, pipe them back in."""
    outcomes: list[dict[str, object]] = []
    with Store(a.db) as s:
        for raw in sys.stdin:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                print(f"skipped (need 3+ tab-separated fields): {line!r}", file=sys.stderr)
                continue
            source = parts[3] if len(parts) > 3 else a.source
            r = assert_belief(
                s,
                parts[0],
                parts[1],
                parts[2],
                valid_from=a.valid_from,
                source=source,
                reliability=a.reliability,
                policy=Policy(a.policy),
            )
            outcomes.append(
                {
                    "subject": parts[0],
                    "relation": parts[1],
                    "value": parts[2],
                    "outcome": r.outcome.value,
                    "belief_id": r.belief_id,
                }
            )
    if getattr(a, "json", False):
        print(json.dumps({"asserted": len(outcomes), "outcomes": outcomes}, indent=2, default=str))
    else:
        for o in outcomes:
            print(f"{o['outcome']}: {o['subject']} {o['relation']} = {o['value']}")
        print(f"({len(outcomes)} asserted)")
    return 0


def _retract_ids(a: argparse.Namespace) -> list[int] | None:
    """The belief ids after `beliefs retract`, or None when the words there are not all ids (a
    key whose subject is "retract" stays readable as history)."""
    words = [w for w in (a.relation, *a.ids) if w is not None]
    return [int(w) for w in words] if all(w.isdigit() for w in words) else None


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


def cmd_beliefs(a: argparse.Namespace) -> int:
    if a.subject == "retract" and _retract_ids(a) is not None:
        return _beliefs_retract(a)
    if a.orphaned or a.apply:
        print("--orphaned and --apply belong to `memware beliefs retract`", file=sys.stderr)
        return 2
    if a.ids:
        print(f"unexpected arguments: {' '.join(a.ids)}", file=sys.stderr)
        return 2
    with Store(a.db) as s:
        if a.stale:
            if a.relation:
                print("--stale takes a subject, not a key", file=sys.stderr)
                return 2
            _emit(a, _stale(s, _gate(a), a.subject), _STALE_COLS)
            return 0
        rows = history(s, a.subject, a.relation) if a.relation else current(s, a.subject)
        _emit(a, rows, _BELIEF_COLS)
    return 0


def cmd_read(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        _emit(a, read_turns(s, a.session, around=a.around, window=a.window), _TURN_COLS)
    return 0


def cmd_review(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        if a.action == "list":
            _out([r.__dict__ for r in open_reviews(s)], a.json)
        elif a.action in ("approve", "reject"):
            try:
                result = (approve if a.action == "approve" else reject)(s, a.id)
            except LookupError as e:  # no open review, or a candidate that cannot be approved
                print(str(e).strip("'\""), file=sys.stderr)
                return 2
            _out(result.__dict__, a.json)
        elif a.action == "sync":
            backend: HttpReviewBackend | JsonlReviewBackend = (
                HttpReviewBackend(a.url, a.token)
                if a.url
                else JsonlReviewBackend(a.outbox, a.inbox)
            )
            _out(sync_reviews(s, backend), a.json)
    return 0


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


def _n(value: int | str) -> str:
    """A count with thousands separators; text as it is."""
    return f"{value:,}" if isinstance(value, int) else value


def _plural(n: int, noun: str, verb: str = "") -> str:
    """``1 turn contains``, ``2 turns contain``: a count, its noun and, if given, the verb."""
    phrase = f"{n:,} {noun}{'' if n == 1 else 's'}"
    return f"{phrase} {verb}{'s' if n == 1 else ''}" if verb else phrase


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


_ASK = "\0ask"
"""The value argparse stores for a text option given without its text: read it from --value-file,
a prompt that does not echo, or standard input."""

_TEXT_FLAGS = ("containing", "turns_containing", "turns_starting_with")


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


def _not_retractable(s: Store, ids: list[int]) -> list[str]:
    """Why each of ``ids`` cannot be retracted by id: no committed belief, or one that is no
    longer current. A superseded belief reaches no prompt already, and retracting it would move
    the end of its interval, which is history."""
    marks = ",".join("?" * len(ids))
    rows = {
        r["id"]: r
        for r in s.conn.execute(
            f"SELECT id, status, valid_to, superseded_by FROM belief WHERE id IN ({marks})", ids
        )
    }
    out = []
    for i in ids:
        r = rows.get(i)
        if r is None or r["status"] != "committed":
            out.append(f"no committed belief with id {i}")
        elif r["valid_to"] is not None:
            by = f" by #{r['superseded_by']}" if r["superseded_by"] else ""
            out.append(
                f"belief {i} is not current: it was superseded{by} at {r['valid_to']}, and "
                "retract by id only acts on current beliefs"
            )
    return out


def _beliefs_retract(a: argparse.Namespace) -> int:
    ids = _retract_ids(a) or []
    if a.orphaned + a.stale + bool(ids) != 1:
        print(
            "beliefs retract needs --orphaned (beliefs whose cited session is no longer indexed), "
            "--stale (beliefs injection leaves out) or belief ids, and takes one of them",
            file=sys.stderr,
        )
        return 2
    with Store(a.db) as s:
        if a.orphaned:
            plan = retract(s, reason="memware beliefs retract --orphaned", apply=a.apply)
            _cascade(a, plan, a.apply)
            return 0
        if a.stale:
            chosen = {
                r["id"]: f"left out of injection, {label(r['reason'])}: {r['why']} "
                "(memware beliefs retract --stale)"
                for r in _stale(s, _gate(a))
            }
        else:
            why = f"retracted by id (memware beliefs retract {' '.join(map(str, ids))})"
            chosen = dict.fromkeys(ids, why)
            refused = _not_retractable(s, ids)
            if refused:
                print("\n".join([*refused, "nothing written"]), file=sys.stderr)
                return 2
        plan = retract(s, reason="", apply=a.apply, beliefs=chosen)
    _cascade(a, plan, a.apply, by_session=False)
    return 0


STALE_DERIVE_DAYS = 30


def _stats_verdicts(r: dict[str, Any]) -> list[str]:
    """One line per degenerate state a person should act on, from a ``cmd_stats`` report. Human
    output only: under --json the fields carry the same facts. The case this exists for is a
    store with thousands of turns and no beliefs, where "derive never ran" and "derive found
    nothing" otherwise print the same zero."""
    d, u = r["derive"], r["utilization"]
    out: list[str] = []
    if r["turns"] and not r["beliefs_current"] and not d["runs"]:
        out.append(
            "ledger empty: derive has never run. `memware derive --plan` previews with no "
            "network call; `memware config derive.auto true` enables it."
        )
    age = d["last_run_age_hours"]
    pending = d["turns_pending"]
    if not d["auto"] and age is not None and age > STALE_DERIVE_DAYS * 24 and pending:
        out.append(
            f"derive last ran {int(age // 24)} days ago and derive.auto is off "
            f"({pending:,} turn{'' if pending == 1 else 's'} not yet derived). "
            "`memware derive --apply` catches up; `memware config derive.auto true` keeps it "
            "current."
        )
    if r["turns"] and not u["beliefs_recalled_30d"] and not u["turns_recalled_30d"]:
        out.append("nothing has been recalled in 30 days")
    orphaned = u["beliefs_orphaned"]
    if orphaned:
        out.append(
            f"{orphaned:,} {'belief cites' if orphaned == 1 else 'beliefs cite'} a session that is "
            "no longer indexed. `memware beliefs retract --orphaned` lists them; add --apply to "
            "retract."
        )
    c = r["capture"]
    hiding = c["exclude"] and _hiding_verdict(c["transcripts_excluded"], c["transcripts"])
    if hiding:
        out.append(hiding)
    return out


def _when(ts: str | None, age: float | None, never: str) -> str:
    if not ts:
        return never
    if age is None:
        return ts
    ago = f"{age:.1f} hours" if age < 48 else f"{int(age // 24)} days"
    return f"{ts} ({ago} ago)"


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def _provenance_lines(p: dict[str, Any]) -> list[tuple[str, str]]:
    """Sessions, turns and beliefs by entrypoint, then the project directories with the most
    sessions: a generator that wrote much of the store is visible without an audit."""
    home = str(Path.home())
    lines: list[tuple[str, str]] = []
    for g in p["by_entrypoint"]:
        label = f"entrypoint {g['entrypoint']}" if g["entrypoint"] else "no entrypoint"
        value = ", ".join(_count(g[k], k[:-1]) for k in ("sessions", "turns", "beliefs")) + (
            "" if g["entrypoint"] else " (derive reads these as interactive)"
        )
        lines.append((label, value))
    for d in p["top_projects"]:
        where = (
            "~" + d["directory"][len(home) :]
            if d["directory"].startswith(home + "/")
            else d["directory"]
        )
        lines.append(
            ("project directory", f"{_count(d['sessions'], 'session')} ({d['share']:.1%}) {where}")
        )
    return lines


def _print_stats(r: dict[str, Any]) -> None:
    """Labeled ``field : value`` lines, a blank line between sections, verdicts last."""
    d, u = r["derive"], r["utilization"]
    share = u["turns_ever_recalled_share"]
    sections: list[list[tuple[str, str]]] = [
        [
            ("db", r["db"]),
            ("turns", f"{r['turns']:,}"),
            ("passages", f"{r['passages']:,}"),
            ("sessions", f"{r['sessions']:,}"),
            ("beliefs current", f"{r['beliefs_current']:,}"),
            ("beliefs total", f"{r['beliefs_total']:,}"),
            ("reviews open", f"{r['reviews_open']:,}"),
        ],
        _provenance_lines(r["provenance"]),
        [
            ("derive auto", "on" if d["auto"] else "off"),
            ("derive sources", d["sources"]),
            ("derive state file", d["state_file"]),
            ("derive runs", f"{d['runs']:,}"),
            ("derive last run", _when(d["last_run"], d["last_run_age_hours"], "never run")),
            ("derive watermark", f"{d['watermark']:,}"),
            ("latest turn id", f"{d['max_turn_id']:,}"),
            ("turns not yet derived", f"{d['turns_pending']:,}"),
        ],
        [
            ("beliefs recalled in 7 days", f"{u['beliefs_recalled_7d']:,}"),
            ("beliefs recalled in 30 days", f"{u['beliefs_recalled_30d']:,}"),
            ("turns recalled in 7 days", f"{u['turns_recalled_7d']:,}"),
            ("turns recalled in 30 days", f"{u['turns_recalled_30d']:,}"),
            (
                "turns ever recalled",
                f"{u['turns_ever_recalled']:,} of {r['turns']:,}"
                + ("" if share is None else f" ({share:.1%})"),
            ),
            ("last recalled", _when(u["last_recalled"], u["last_recalled_age_hours"], "never")),
            ("beliefs citing an unindexed session", f"{u['beliefs_orphaned']:,}"),
            ("beliefs with a stale turn citation", f"{u['beliefs_stale_turn']:,}"),
        ],
        _injection_lines(r["injection"]),
        _capture_lines(r["capture"]),
        [("verdict", v) for v in _stats_verdicts(r)],
    ]
    blocks = [section for section in sections if section]
    width = max(len(label) for block in blocks for label, _ in block)
    print(
        "\n\n".join(
            "\n".join(f"{label.rjust(width)} : {value}" for label, value in block)
            for block in blocks
        )
    )


def _injection_status(s: Store, gate: Gate) -> dict[str, Any]:
    """Current beliefs the injection gate leaves out, by reason, and what decided it: the window
    and, for the project in the current directory, the versions its manifests declare."""
    left_out = dict.fromkeys(REASONS, 0)
    for row in _stale(s, gate):
        left_out[row["reason"]] += 1
    return {
        "volatile_days": gate.volatile_days,
        "manifests": [d._asdict() for d in gate.declared],
        "left_out": left_out,
    }


def _injection_lines(i: dict[str, Any]) -> list[tuple[str, str]]:
    total = sum(i["left_out"].values())
    parts = ", ".join(f"{n:,} {label(k)}" for k, n in i["left_out"].items() if n)
    days = i["volatile_days"]
    lines = [
        (
            "beliefs left out of injection",
            f"{total:,}" + (f" ({parts}; `memware beliefs --stale` lists them)" if total else ""),
        ),
        (
            "inject.volatile_days",
            f"{days:g}: a volatile derived belief is injected while younger than that"
            if days
            else "0: a derived measurement, moving version or status is never injected",
        ),
    ]
    if i["manifests"]:
        declared = "; ".join(
            f"{d['name'] or '(no name)'} {d['version']} ({d['path']})" for d in i["manifests"]
        )
        lines.append(("manifest versions here", declared))
    return lines


def _capture_status() -> dict[str, Any]:
    """``capture.exclude`` and how many transcripts on disk it hides. The transcript tree is walked
    only when a pattern is set, so a store with no exclusions pays nothing."""
    from memware.config import get_dotted, load_config
    from memware.ingest import capture_exclude_patterns, is_excluded

    patterns = capture_exclude_patterns()
    out: dict[str, Any] = {"exclude": patterns, "transcripts": None, "transcripts_excluded": None}
    if patterns:
        src = str(get_dotted(load_config(), "backup.transcript_src") or "~/.claude/projects")
        disk = _transcripts_on_disk(src)
        out["transcripts"] = len(disk)
        out["transcripts_excluded"] = sum(is_excluded(p, patterns) for p in disk)
    return out


def _capture_lines(c: dict[str, Any]) -> list[tuple[str, str]]:
    if not c["exclude"]:
        return []
    n = len(c["exclude"])
    return [
        ("capture.exclude", f"{n} pattern{'' if n == 1 else 's'} (`memware exclude` lists them)"),
        ("transcripts excluded", _of(c["transcripts_excluded"], c["transcripts"])),
    ]


def cmd_stats(a: argparse.Namespace) -> int:
    _maybe_setup_hint(a)
    with Store(a.db) as s:
        report: dict[str, Any] = {"db": str(s.path), **s.stats()}
        report["provenance"] = s.provenance()
        report["derive"] = derive_status(s.conn, s.path)
        report["utilization"] = {
            **s.utilization(),
            "beliefs_orphaned": orphaned_count(s),
            "beliefs_stale_turn": stale_turn_count(s),
        }
        report["injection"] = _injection_status(s, injection_gate(resolve_project(Path.cwd())))
    report["capture"] = _capture_status()
    if a.json:
        _out(report, True)
    else:
        _print_stats(report)
    return 0


def _resolve_backup_dest(a: argparse.Namespace) -> str | None:
    from memware.config import get_dotted, load_config

    dest: str | None = a.dest or get_dotted(load_config(), "backup.dest")
    return dest


def cmd_backup(a: argparse.Namespace) -> int:
    from memware import backup as bk
    from memware.config import get_dotted, load_config

    cfg = load_config()
    dest = a.dest or get_dotted(cfg, "backup.dest")
    if not dest:
        if a.if_stale is not None:
            return 0  # hook-driven: no destination configured yet, stay silent
        print(
            "no backup destination — pass --dest DIR or run `memware setup` "
            "(point it at a Dropbox/iCloud/Drive folder or an external disk)",
            file=sys.stderr,
        )
        return 2
    if a.if_stale is not None:
        # throttle: skip if a snapshot already exists within the window (so this is safe to
        # call from every session-end hook — the backup rides usage, not a clock).
        if not get_dotted(cfg, "backup.auto") and not a.dest:
            return 0
        age = bk.newest_age_hours(dest)
        if age is not None and age < a.if_stale:
            if not a.quiet:
                _out({"skipped": "recent", "age_hours": round(age, 1)}, a.json)
            return 0
    keep = a.keep or get_dotted(cfg, "backup.keep_days") or [1, 3, 7, 14]
    out = bk.snapshot(a.db, dest)
    deleted = bk.apply_retention(dest, keep)
    result: dict[str, object] = {
        "snapshot": str(out),
        "kept": [p.name for p in bk.list_snapshots(dest)],
        "pruned": [p.name for p in deleted],
    }
    include = a.transcripts or (
        a.transcripts is None and bool(get_dotted(cfg, "backup.include_transcripts"))
    )
    if include:
        src = a.transcript_src or get_dotted(cfg, "backup.transcript_src") or "~/.claude/projects"
        mirrored = bk.mirror_transcripts(src, dest)
        result["transcripts_mirrored"] = mirrored.copied
        result["transcripts_skipped_no_capture"] = len(mirrored.excluded_no_capture)
        result["transcripts_skipped_glob"] = len(mirrored.excluded_glob)
        result["transcripts_skipped_marker"] = len(mirrored.excluded_marker)
        hiding = _hiding_verdict(len(mirrored.excluded_glob), mirrored.seen)
        if hiding:
            print(hiding, file=sys.stderr)
        if mirrored.left_in_backup:
            # Copies an earlier run made before the transcript was listed, excluded or marked. memware
            # never deletes from a destination, so a person has to; say where, every run.
            result["transcripts_left_in_backup"] = [str(p) for p in mirrored.left_in_backup]
            for target in mirrored.left_in_backup[:5]:
                print(
                    f"excluded transcript already in the backup, remove it by hand: {target}",
                    file=sys.stderr,
                )
            if len(mirrored.left_in_backup) > 5:
                print(f"... and {len(mirrored.left_in_backup) - 5} more", file=sys.stderr)
        if mirrored.skipped:
            # Reported, not fatal: the snapshot above already succeeded and the mirror is
            # retried on every run. Silence would hide a destination that never takes
            # writes; a traceback took five nightly crons down over one dataless file.
            result["transcripts_skipped"] = len(mirrored.skipped)
            for target, why in mirrored.skipped[:5]:
                print(f"transcript not mirrored: {target}: {why}", file=sys.stderr)
            if len(mirrored.skipped) > 5:
                print(f"... and {len(mirrored.skipped) - 5} more", file=sys.stderr)
    if not a.quiet:
        _out(result, a.json)
    return 0


def cmd_restore(a: argparse.Namespace) -> int:
    from memware import backup as bk

    dest = _resolve_backup_dest(a)
    snap = a.from_file
    if not snap:
        if not dest:
            print("no backup destination configured; pass --from FILE", file=sys.stderr)
            return 2
        snaps = bk.list_snapshots(dest)
        if not snaps:
            print(f"no snapshots in {dest}", file=sys.stderr)
            return 2
        snap = str(snaps[0])
    prev = bk.restore(snap, a.db)
    with Store(a.db) as s:
        stats = s.stats()
    _out({"restored_from": snap, "previous_store_saved_to": str(prev), **stats}, a.json)
    return 0


def _prompt(msg: str, default: str = "") -> str:
    """input() that returns ``default`` on a closed stdin, so setup is safe non-interactively."""
    try:
        return input(msg).strip()
    except EOFError:
        return default


def _ask(msg: str, *, default_yes: bool) -> bool | None:
    """A yes/no question; None on a closed stdin, for when no answer must change nothing."""
    try:
        ans = input(f"{msg} {'[Y/n]' if default_yes else '[y/N]'}: ").strip().lower()
    except EOFError:
        return None
    return default_yes if not ans else ans[0] == "y"


def _yes(msg: str, *, default_yes: bool = True) -> bool:
    ans = _ask(msg, default_yes=default_yes)
    return default_yes if ans is None else ans


# Features that send data somewhere new, by the version that added them. A setup run on an
# older version never asked about them, so the hint re-asks until setup runs on that version or
# later, or the feature's `<name>.auto` switch has been written either way.
CONSENT: dict[str, str] = {"0.4.0": "derive"}
_CONSENT_HINTS = {
    "derive": "memware {version} added `derive`, which sends transcript excerpts to a model. "
    "Run `memware setup` to enable or decline; `memware derive --plan` previews with no "
    "network call.",
}


def _older(version: str, than: str) -> bool:
    """Whether ``version`` is older than ``than``, compared as versions, never as strings
    (as strings, "0.10.0" sorts before "0.4.0"). A version that cannot be read is older: setup
    never recorded one it could read, so it never asked."""
    return older_version(version, than) is not False


def _consent_hints(done: object) -> list[str]:
    """The hint for each CONSENT feature setup has not asked about — setup never ran (``done`` is
    empty), or last ran on an older version — and whose switch was never written."""
    from memware.config import has_key

    return [
        _CONSENT_HINTS[feature].format(version=version)
        for version, feature in CONSENT.items()
        if (not done or _older(str(done), version)) and not has_key(f"{feature}.auto")
    ]


def _maybe_setup_hint(a: argparse.Namespace) -> None:
    """One-line nudges to `memware setup`, on stderr; silent from hooks and in --json mode.

    Backups: for anyone who has never configured them — new installs and upgrades from a
    pre-backup (pre-0.2) version alike; stops once setup has run or a destination is set.
    Consent: see ``_consent_hints``; `memware notice` carries the same lines to plugin users."""
    from memware.config import get_dotted, load_config

    if getattr(a, "from_hook", False) or getattr(a, "json", False):
        return
    cfg = load_config()
    done = get_dotted(cfg, "setup.completed_version")
    if not done and not get_dotted(cfg, "backup.dest"):
        print(
            "Tip: run `memware setup` to configure backups (one time; this hint then stops).",
            file=sys.stderr,
        )
    for hint in _consent_hints(done):
        print(hint, file=sys.stderr)


STALE_NOTICE = "stale-beliefs"
"""The key in the store's ``notice`` table that records the stale-belief notice was given."""


def _notice_given(db: str, key: str) -> bool:
    """Whether the store records notice ``key``, read through a handle that takes no lock. A
    store older than the ``notice`` table has not given it."""
    try:
        conn = open_readonly(db)
    except (OSError, sqlite3.Error):
        return False
    try:
        return conn.execute("SELECT 1 FROM notice WHERE key=?", (key,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _stale_notice(a: argparse.Namespace, cwd: object) -> list[str]:
    """Once per store: how many beliefs injection now leaves out, and the command that lists
    them. Every session start after the first is one read that takes no lock. The first counts,
    and records in the store's ``notice`` table that it ran, whether or not it had anything to
    say; that write waits at most a quarter second for another writer and is skipped rather than
    stall the session start (the next one tries again). A marker in the store, not the memware
    home, so a home that will not parse or take a write changes nothing. A store that is not
    there is never created."""
    if a.db == ":memory:" or not Path(a.db).expanduser().exists():
        return []  # no store yet: nothing was ever injected, and a hook must not create one
    if _notice_given(a.db, STALE_NOTICE):
        return []
    store = _hook_store(a.db)
    if store is None:
        return []  # locked mid-upgrade: the next session start says it
    with store as s:
        n = len(_stale(s, injection_gate(resolve_project(Path(str(cwd or os.getcwd()))))))
        try:
            claimed = s.conn.execute(
                "INSERT OR IGNORE INTO notice(key, shown_at, version) VALUES (?,?,?)",
                (STALE_NOTICE, now_iso(), __version__),
            ).rowcount
        except sqlite3.OperationalError:  # locked: say it now, record it next time
            claimed = 1
    if not n or not claimed:
        return []  # nothing to say, or a session starting alongside said it
    return [
        f"memware no longer injects {_count(n, 'belief')} that "
        f"{'was true when recorded and needs' if n == 1 else 'were true when recorded and need'} "
        "re-checking now (a measurement, a moving version or a status, or a version the project "
        "manifest overrules). `memware beliefs --stale` lists "
        f"{'it' if n == 1 else 'them'} and why; `memware beliefs retract --stale --apply` "
        f"retracts {'it' if n == 1 else 'them'}."
    ]


def cmd_notice(a: argparse.Namespace) -> int:
    """What the person should hear at session start, for someone who only uses the plugin: they
    never type a memware command, so they never see what `stats` prints. The plugin runs this in
    the foreground at session start, and Claude Code shows the ``systemMessage`` to them. The
    consent hints read the config file. The stale-belief notice reads the store's one-time marker
    without a lock, counts only the first time, and never creates a store. Whatever goes wrong, it prints nothing and exits 0: it cannot fail a session
    start."""
    try:
        from memware.config import config_path, get_dotted

        payload = _hook_payload() if a.from_hook else {}
        if payload.get("source") == "compact":
            return 0  # a compaction mid-session, not a session the person just opened
        # No file means nobody has answered. A file that will not parse is not an answer either,
        # but it may hold one: stay quiet rather than ask at every session start.
        path = config_path()
        user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(user, dict):
            return 0
        hints = _consent_hints(get_dotted(user, "setup.completed_version"))
        with contextlib.suppress(Exception):
            hints += _stale_notice(a, payload.get("cwd"))
        if a.from_hook:
            if hints:
                print(json.dumps({"systemMessage": "\n".join(hints)}))
        else:
            _out(hints, a.json)
    except Exception:
        pass
    return 0


def _derive_destination(cfg: dict[str, Any]) -> str:
    """Where `memware derive` sends excerpts, resolved the way a run resolves it but without
    building a provider (which needs `claude` on PATH or a key)."""
    from urllib.parse import urlparse

    from memware.config import get_dotted
    from memware.derive import read_env

    env = read_env()
    provider = env.get("MEMWARE_DERIVE_PROVIDER") or get_dotted(cfg, "derive.provider")
    model = get_dotted(cfg, "derive.model") or env.get("MEMWARE_DERIVE_MODEL")
    if provider == "openai":
        base = env.get("OPENAI_BASE_URL")
        host = (urlparse(base).netloc or base) if base else "OPENAI_BASE_URL (not set)"
        model = model or env.get("OPENAI_MODEL") or "OPENAI_MODEL (not set)"
        return f"{model} at {host}, an OpenAI-compatible endpoint"
    return f"{model or 'haiku'} via the Claude Code CLI (`claude -p`) on your own subscription"


def cmd_setup(a: argparse.Namespace) -> int:
    """Guided one-time configuration: index the sessions already on disk (new installs),
    choose a backup destination, run a first backup, ask whether to switch on automatic derive,
    and print the operating guidance. Safe to re-run, and safe non-interactive — a closed stdin
    (or ``--yes``) keeps every current value, so derive is never switched on without an answer.
    Covers a fresh install and an upgrade from a version that never asked alike."""
    from memware import backup as bk
    from memware.config import (
        get_dotted,
        has_key,
        load_config,
        load_user_config,
        save_config,
        set_dotted,
    )

    # Read the merged view; write only what setup decides. Saving the merged view would record
    # every default as a choice — derive.auto false as a decline nobody made.
    cfg, user = load_config(), load_user_config()

    def put(key: str, value: object) -> None:
        set_dotted(cfg, key, value)
        set_dotted(user, key, value)

    yes = getattr(a, "yes", False)
    src_default = get_dotted(cfg, "backup.transcript_src") or "~/.claude/projects"

    with Store(a.db) as s:
        stats = s.stats()
    fresh = stats["turns"] == 0
    print("memware setup\n")
    if fresh:
        print("This store is empty. The plugin captures new sessions from now on; you can also")
        print("index the transcripts already on disk so recall works over past work today.")
    else:
        print(f"This store holds {stats['turns']:,} turns from {stats['sessions']:,} sessions.")
        print("Let's make sure backups are configured so an aged session can't be lost.")

    # 1. Backfill existing transcripts (mainly a fresh install / new machine).
    root = Path(src_default).expanduser()
    if (
        fresh
        and root.exists()
        and (yes or _yes(f"\nIndex existing sessions in {src_default} now?"))
    ):
        with Store(a.db) as s:
            report = sync_tree(s, root, harness="claude-code")
            stats = s.stats()
        print(
            f"  indexed {sum(report.values()):,} turns from {len(report)} files "
            f"({stats['sessions']:,} sessions)."
        )

    # 2. Backup destination.
    print("\nBackups: pick a folder your OS already syncs, or a drive you keep — memware just")
    print("writes there (Dropbox, iCloud Drive, Google Drive, an external disk, a network mount).")
    print("Snapshots are a rolling 1/3/7/14-day set you can revert to; raw transcripts are")
    print("mirrored separately so a session outlives your OS's ~30-day transcript cleanup.")
    cur = get_dotted(cfg, "backup.dest")
    if cur:
        print(f"  Current: {cur}")
    dest = "" if yes else _prompt("Backup folder (blank to keep current / skip): ")
    if dest:
        put("backup.dest", dest)
    dest = get_dotted(cfg, "backup.dest")
    if dest:
        put(
            "backup.include_transcripts",
            True if yes else _yes("Also mirror raw transcripts there (recommended)?"),
        )

    # 3. Persist the backup choices before the first backup touches the destination.
    save_config(user)

    # 4. Offer a first backup right now.
    if dest and (yes or _yes("Run a first backup now?")):
        dpath = Path(dest).expanduser()
        out = bk.snapshot(a.db, dpath)
        bk.apply_retention(dpath, get_dotted(cfg, "backup.keep_days") or [1, 3, 7, 14])
        n = (
            bk.mirror_transcripts(
                get_dotted(cfg, "backup.transcript_src") or src_default, dpath
            ).copied
            if get_dotted(cfg, "backup.include_transcripts")
            else 0
        )
        print(f"  snapshot {Path(out).name}" + (f", {n} transcripts mirrored" if n else ""))

    # 5. Derive sends transcript excerpts to a model, and a transcript can hold anything, so it
    # stays off until a person answers yes here. --yes and a closed stdin change nothing.
    print("\nDerive: `memware derive` sends sentences from your transcripts that look like durable")
    print("facts, with their neighbours, to a model, and files the facts it finds as beliefs.")
    print(f"Excerpts go to {_derive_destination(cfg)}.")
    print("Preview what would be sent, with no network call: `memware derive --plan`.")
    auto = bool(get_dotted(cfg, "derive.auto"))
    if yes:
        print(f"  Automatic derive stays {'on' if auto else 'off'}: --yes never changes it.")
    else:
        if has_key("derive.auto"):
            print(f"  Current: {'on' if auto else 'off'}")
        answer = _ask(
            "Enable automatic derive (runs at session start, at most once a day)?",
            default_yes=auto,
        )
        if answer is None:  # closed stdin: the prompt is still on the line
            print(f"\n  Automatic derive stays {'on' if auto else 'off'}: no answer.")
        else:
            put("derive.auto", answer)  # a decline is written too, so the consent hint stops
            print(
                f"  Automatic derive {'on' if answer else 'off'}; change it any time with "
                f"`memware config derive.auto {'false' if answer else 'true'}`."
            )

    # 6. Persist, and mark setup done so the discovery hints stop.
    put("setup.completed_version", __version__)
    print(f"\nSaved {save_config(user)}.")

    # 7. Operating guidance.
    print("\nHow backups keep running:")
    if dest:
        print("  • The Claude Code plugin backs up at session end, at most once every ~20h — no")
        print("    cron, and never missed by a laptop sleeping through a scheduled time.")
        print("  • Always-on machine without the plugin? Schedule `memware backup` (launchd on")
        print("    macOS, systemd on Linux; avoid plain cron on a laptop). See docs/backup.md.")
    else:
        print("  • No destination set — recall still works, but there's no wipe-trap safety net.")
        print("    Re-run `memware setup` any time to add one.")
    print("  • Sensitive session? Start Claude Code with MEMWARE_NO_CAPTURE=1. The plugin's hooks")
    print("    list its transcript, so no sync indexes it and no backup mirrors it. A session no")
    print("    memware hook ran in cannot be recognised; `claude -p --no-session-persistence`")
    print("    writes no transcript at all. See docs/keeping-memory-clean.md.")
    print("  • After a wipe, `memware restore --latest` — never wipe-and-re-backfill (backfill")
    print("    only re-indexes transcripts still on disk). See docs/backup.md.")
    return 0


def _relevance_notice(mode: str) -> None:
    """What switching the relevance filter on sends, and where, before the first prompt does."""
    from memware.derive import env_file

    print(
        f"relevance.mode {mode}: from the next prompt, the prompt hook and the Hermes provider "
        f"send each prompt and its candidate facts to TypeSafe ({relevance.ENDPOINT}); "
        "`memware config relevance.mode off` stops it",
        file=sys.stderr,
    )
    if relevance.api_key() is None:
        print(
            f"no {relevance.KEY} in the environment or {env_file()}: until one is set, nothing "
            "is sent and injection is unchanged",
            file=sys.stderr,
        )


def cmd_config(a: argparse.Namespace) -> int:
    from memware.config import (
        config_path,
        get_dotted,
        load_config,
        load_user_config,
        save_config,
        set_dotted,
    )

    if a.key and a.value is not None:
        val: object = a.value
        if a.key.endswith("keep_days"):
            val = [int(x) for x in a.value.replace(",", " ").split()]
        elif a.key == WINDOW_KEY:
            days = parse_days(a.value)
            if days is None:
                print(
                    f"{WINDOW_KEY} takes a number of days, 0 or more (0 never injects a volatile "
                    f"belief); got {a.value!r}, nothing written",
                    file=sys.stderr,
                )
                return 2
            val = int(days) if days.is_integer() else days
        elif a.key.startswith("relevance."):
            parsed = relevance.parse_setting(a.key, a.value)
            name = a.key.removeprefix("relevance.")
            if parsed is None:
                expects = relevance.EXPECTS.get(name)
                print(
                    f"{a.key} takes {expects}"
                    if expects
                    else f"{a.key} is not a setting; relevance takes {', '.join(relevance.EXPECTS)}",
                    f"; got {a.value!r}, nothing written",
                    sep="",
                    file=sys.stderr,
                )
                return 2
            val = parsed
            if name == "mode" and val != "off":
                _relevance_notice(str(val))
        elif a.value.lower() in ("true", "false"):
            val = a.value.lower() == "true"
        user = load_user_config()  # write the one key; defaults stay defaults, not choices
        set_dotted(user, a.key, val)
        save_config(user)
        _out({a.key: get_dotted(user, a.key), "path": str(config_path())}, a.json)
    elif a.key:
        _out({a.key: get_dotted(load_config(), a.key)}, a.json)
    else:
        _out({**load_config(), "path": str(config_path())}, a.json)
    return 0


NUKE_PHRASE = "DELETE ALL MEMWARE DATA"


def cmd_nuke(a: argparse.Namespace) -> int:
    """Permanently delete the store, its config, review files, AND every snapshot in the
    backup destination. Guarded by a typed confirmation so it cannot happen by accident."""
    from memware import backup as bk
    from memware.config import config_path, get_dotted, load_config, memware_home

    dest = get_dotted(load_config(), "backup.dest")
    snaps = bk.list_snapshots(dest) if dest else []
    targets = [Path(a.db).expanduser(), Path(str(a.db) + "-wal"), Path(str(a.db) + "-shm")]
    home = memware_home()
    for name in (
        "ignore-markers.txt",
        "no-capture.txt",
        "no-capture.txt.lock",
        "review-outbox.jsonl",
        "review-inbox.jsonl",
        relevance.LOG_NAME,  # holds the text of prompts, when the relevance filter was on
        relevance.USAGE_NAME,
    ):
        targets.append(home / name)
    targets.append(config_path())
    print("This permanently deletes:")
    print(f"  store:      {a.db} (+ wal/shm)")
    print(f"  config:     {config_path()}")
    print(
        f"  snapshots:  {len(snaps)} in {dest}"
        if dest
        else "  snapshots:  (no backup dest configured)"
    )
    print(f"\nType exactly:  {NUKE_PHRASE}")
    typed = a.confirm
    if typed is None:
        try:
            typed = input("> ").strip()
        except EOFError:
            typed = ""
    if typed != NUKE_PHRASE:
        print("phrase did not match — nothing deleted", file=sys.stderr)
        return 1
    removed = 0
    for snap in snaps:
        snap.unlink(missing_ok=True)
        removed += 1
    for t in targets:
        if t.exists():
            t.unlink()
            removed += 1
    _out({"deleted_files": removed, "snapshots_deleted": len(snaps)}, a.json)
    return 0


_DESCRIPTION = (
    "Memory for AI agents that only remembers the latest truth — a local SQLite belief "
    "ledger and transcript index. No daemon, no vector database, and no model in the loop "
    "unless you opt in to the relevance filter."
)

_EPILOG = """\
Examples:
  memware backfill                          index the sessions already on disk (run once)
  memware recall "which port" "api port"    search; pass several phrasings, they are fused
  memware recall "db url" --plain | fzf     scriptable, tab-separated, one hit per line
  memware beliefs api                       current beliefs about a subject
  memware assert api "listens on" 8443      record a fact (supersedes the old value)
  memware setup                             guided backups, derive opt-in, first-run config
  memware completions zsh > ~/.zfunc/_memware      install shell completion

Environment:
  MEMWARE_DB          store path (default: <home>/memware.db)
  MEMWARE_DERIVE_PROVIDER / MEMWARE_DERIVE_MODEL   `derive`: claude-code (default) | openai, and its model
  OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY   `derive --provider openai` (or <home>/.env)
  MEMWARE_HOME        config/store dir (default: ~/.memware, else $XDG_DATA_HOME/memware)
  MEMWARE_ASCII=1     ASCII-only output (also on when the locale is not UTF-8); same as --ascii
  MEMWARE_NO_CAPTURE=1  never index or mirror this session (needs a memware hook to run in it)
  NO_COLOR            honoured by construction — memware emits no colour at all

Files:
  <home>/config.json          configuration (see `memware config`)
  <home>/ignore-markers.txt   content signatures never to index or mirror
  <home>/no-capture.txt       transcripts of MEMWARE_NO_CAPTURE sessions, never indexed or mirrored

Accessibility:
  No information is ever conveyed by colour. --plain gives tab-separated records for scripts
  and screen readers; --ascii avoids non-ASCII glyphs. See docs/accessibility.md.

See also: man memware  ·  https://github.com/ericwalisko/memware
"""


def cmd_completions(a: argparse.Namespace) -> int:
    """Print a shell completion script for bash/zsh/fish (generated from the parser by shtab)."""
    try:
        import shtab
    except ImportError:
        print(
            "shell completions need shtab:  pip install 'memware[shell]'  (or: pip install shtab)",
            file=sys.stderr,
        )
        return 2
    print(shtab.complete(build_parser(), shell=a.shell))
    return 0


def build_parser() -> argparse.ArgumentParser:
    from memware.config import memware_home

    p = argparse.ArgumentParser(
        prog="memware", description=_DESCRIPTION, epilog=_EPILOG, formatter_class=_HelpFormatter
    )
    default_db = os.environ.get("MEMWARE_DB") or str(memware_home() / "memware.db")
    p.add_argument("--db", default=default_db, metavar="FILE", help="SQLite store (env MEMWARE_DB)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument(
        "--plain",
        action="store_true",
        help="tab-separated records, one per line (scriptable; pipe to fzf/awk/cut)",
    )
    p.add_argument(
        "--ascii",
        action="store_true",
        help="ASCII only; no non-ASCII glyphs (screen readers, non-UTF-8 terminals)",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add(name: str, help: str, epilog: str | None = None) -> argparse.ArgumentParser:
        sp = sub.add_parser(
            name, help=help, description=help, epilog=epilog, formatter_class=_HelpFormatter
        )
        for flag in ("--json", "--plain", "--ascii"):  # global; documented on the top-level
            sp.add_argument(
                flag, action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
            )
        return sp

    s = add("init", "create the database")
    s.set_defaults(fn=cmd_init)

    s = add(
        "sync",
        "index new turns from transcripts",
        epilog=(
            "Examples:\n"
            "  memware sync                      catch up the configured transcript source\n"
            "  memware sync ~/.claude/projects   index a specific tree (idempotent)"
        ),
    )
    s.add_argument("paths", nargs="*")
    s.add_argument("--harness", default="claude-code")
    s.add_argument("--from-hook", action="store_true")
    s.add_argument(
        "--skip-if-contains",
        metavar="TEXT",
        help="skip (and un-index) files whose head contains TEXT, e.g. an eval marker",
    )
    s.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="path glob to skip when syncing a directory (repeatable)",
    )
    s.set_defaults(fn=cmd_sync)

    s = add(
        "backfill",
        "one-time index of existing transcripts (run once on a new machine)",
        epilog="Example:\n  memware backfill        index ~/.claude/projects once on a new machine",
    )
    s.add_argument(
        "root",
        nargs="?",
        default="~/.claude/projects",
        help="transcript root (default: ~/.claude/projects)",
    )
    s.add_argument("--harness", default="claude-code")
    s.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    s.set_defaults(fn=cmd_backfill)

    s = add(
        "exclude",
        "list, add or remove capture.exclude path globs: transcripts no sync indexes and no "
        "backup mirrors (a dry run unless --apply)",
        epilog=(
            "Examples:\n"
            "  memware exclude                                  each pattern and what it matches\n"
            "  memware exclude --add '*/-Users-me-gen-runs/*'   preview: matches, sources to un-index\n"
            "  memware exclude --add '*/-Users-me-gen-runs/*' --apply\n"
            "  memware exclude --remove '*/-Users-me-gen-runs/*' --apply\n"
            "  memware exclude --add '*privateproj*' --apply    a project, its subdirectories and\n"
            "                                                   worktrees\n"
            "A pattern is matched against the whole resolved transcript path, and * crosses /.\n"
            "Claude Code names a project's directory after its path with every character but a\n"
            "letter or digit a dash: /Users/me/gen-runs is -Users-me-gen-runs, and a worktree of\n"
            "it -Users-me-gen-runs--claude-worktrees-feat. A new pattern holding / that matches\n"
            "nothing is refused unless --force. --apply scrubs the store file as prune --apply\n"
            "does. See docs/keeping-memory-clean.md."
        ),
    )
    which = s.add_mutually_exclusive_group()
    which.add_argument("--add", metavar="GLOB", help="add a pattern")
    which.add_argument("--remove", metavar="GLOB", help="remove a pattern")
    s.add_argument(
        "--apply",
        action="store_true",
        help="write the config, un-index the indexed sources the patterns match, and scrub the "
        "store file",
    )
    s.add_argument(
        "--force",
        action="store_true",
        help="with --apply, add a pattern written as a path even though it matches nothing",
    )
    s.set_defaults(fn=cmd_exclude)

    s = add(
        "recall",
        "search turns and beliefs; pass several phrasings to fuse them",
        epilog=(
            "Examples:\n"
            '  memware recall "which port does the api use" "api port" 8443\n'
            '  memware recall "auth flow" --what beliefs\n'
            '  memware recall "auth flow" --plain | fzf         pick a hit interactively\n'
            '  memware recall "auth flow" --plain | cut -f1      just the ids'
        ),
    )
    s.add_argument(
        "queries",
        nargs="+",
        metavar="QUERY",
        help="one or more phrasings: synonyms, related terms, the literal value you expect",
    )
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--what", choices=["all", "turns", "beliefs"], default="all")
    s.add_argument("--full", action="store_true")
    s.add_argument("--no-touch", action="store_true")
    s.add_argument("--snippet-tokens", type=int, default=96, help="FTS5 snippet window (tokens)")
    s.set_defaults(fn=cmd_recall)

    s = add(
        "context",
        "print the beliefs a prompt names, less the stale ones (hook-friendly)",
    )
    s.add_argument("prompt", nargs="?")
    s.add_argument("-k", type=int, default=6)
    s.add_argument("--from-hook", action="store_true")
    s.set_defaults(fn=cmd_context)

    s = add(
        "notice",
        "print what `memware setup` has not asked about yet (the plugin shows it at session start)",
        epilog=(
            "Examples:\n"
            "  memware notice               one line per pending question; nothing when none\n"
            "  memware notice --from-hook   the same as Claude Code hook JSON (systemMessage)"
        ),
    )
    s.add_argument(
        "--from-hook",
        action="store_true",
        help="print hook JSON with a systemMessage, and nothing after a compaction",
    )
    s.set_defaults(fn=cmd_notice)

    s = add(
        "digest",
        "print this project's recent sessions and beliefs (the SessionStart hook injects it)",
        epilog=(
            "Examples:\n"
            "  memware digest                   what memware holds for this directory's project\n"
            "  memware digest --cwd ~/src/api -k 3\n"
            "  memware digest --from-hook       SessionStart hook JSON; reads the payload on stdin\n"
            "Prints nothing when memware has no session for the project."
        ),
    )
    s.add_argument(
        "--from-hook",
        action="store_true",
        help="read the SessionStart payload on stdin and print hook JSON",
    )
    s.add_argument(
        "--cwd", metavar="DIR", help="project directory; omitted, the hook's cwd, else this one"
    )
    s.add_argument("-k", type=int, default=5, help="most recent sessions to list")
    s.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        metavar="N",
        help="cap on the block; the opening line always prints",
    )
    s.set_defaults(fn=cmd_digest)

    s = add(
        "assert",
        "record a belief; supersedes the previous value",
        epilog=(
            "Examples:\n"
            '  memware assert api "listens on port" 8443 --source "session 3f2a"\n'
            "  printf 'api\\tlistens on port\\t8443\\n' | memware assert -   (batch TSV from stdin)"
        ),
    )
    s.add_argument("subject", help="subject, or `-` to batch-read TSV lines from stdin")
    s.add_argument("relation", nargs="?", help="relation (omit only when subject is `-`)")
    s.add_argument("value", nargs="?", help="value (omit only when subject is `-`)")
    s.add_argument("--valid-from")
    s.add_argument("--source")
    s.add_argument("--reliability", type=float, default=0.5)
    s.add_argument(
        "--policy", choices=[x.value for x in Policy], default=Policy.GATE_CONFLICTS.value
    )
    s.set_defaults(fn=cmd_assert)

    s = add(
        "beliefs",
        "current beliefs, or the history of one key",
        epilog=(
            "Examples:\n"
            "  memware beliefs                        all current beliefs\n"
            "  memware beliefs api                    current beliefs about a subject\n"
            '  memware beliefs api "listens on port"  full history of one key\n'
            "  memware beliefs --stale                what injection leaves out, and why\n"
            "  memware beliefs retract --orphaned     dry run: beliefs whose session is gone\n"
            "  memware beliefs retract --orphaned --apply   retract them (rows are kept)\n"
            "  memware beliefs retract --stale --apply      retract what --stale lists\n"
            "  memware beliefs retract 12 15 --apply        retract beliefs by id"
        ),
    )
    s.add_argument(
        "subject", nargs="?", help="a subject, or `retract` (with --orphaned, --stale or ids)"
    )
    s.add_argument("relation", nargs="?")
    s.add_argument("ids", nargs="*", help=argparse.SUPPRESS)
    s.add_argument(
        "--orphaned",
        action="store_true",
        help="with `retract`: committed beliefs citing a session that is no longer indexed",
    )
    s.add_argument(
        "--stale",
        action="store_true",
        help="current beliefs the prompt hook and digest leave out: a derived measurement, "
        "moving version or status, or a version the project manifest overrules; with `retract`, "
        "retract them",
    )
    s.add_argument(
        "--cwd",
        metavar="DIR",
        help="with --stale: the project whose manifest version is checked (default: this one)",
    )
    s.add_argument(
        "--apply",
        action="store_true",
        help="with `retract`: write the retraction; without it nothing is written",
    )
    s.set_defaults(fn=cmd_beliefs)

    s = add(
        "derive",
        "fill the ledger from the transcripts: durable facts as beliefs (docs/scheduling.md)",
        epilog=(
            "Examples:\n"
            "  memware derive --plan                  no network: every excerpt a run would send, and where\n"
            "  memware derive                         dry run: sends excerpts to the model, writes nothing\n"
            "  memware derive --apply                 file the candidates, advance the watermark\n"
            "  memware config derive.auto true        let the Claude Code plugin run it daily\n"
            "  memware derive --provider openai       any OpenAI-compatible endpoint (OPENAI_* env)\n"
            "Exit: 0 done · 2 not configured · 4 provider unavailable (nothing written, retry later)"
        ),
    )
    _derive_arguments(s)
    s.set_defaults(fn=cmd_derive)

    s = add(
        "read",
        "read a session's turns",
        epilog="Example:\n  memware read <session-id> --around <turn-id> --window 5",
    )
    s.add_argument("session")
    s.add_argument("--around", type=int)
    s.add_argument("--window", type=int, default=5)
    s.set_defaults(fn=cmd_read)

    s = add("review", "list/approve/reject/sync contested supersessions")
    s.add_argument("action", choices=["list", "approve", "reject", "sync"])
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--outbox", default="~/.memware/review-outbox.jsonl")
    s.add_argument("--inbox", default="~/.memware/review-inbox.jsonl")
    s.add_argument("--url")
    s.add_argument("--token")
    s.set_defaults(fn=cmd_review)

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

    s = add(
        "stats",
        "counts, derive state, and recall utilization; says plainly when memory is inert",
    )
    s.set_defaults(fn=cmd_stats)

    s = add(
        "backup",
        "snapshot the store to a folder (Dropbox/iCloud/Drive/disk) with tiered retention",
        epilog=(
            "Examples:\n"
            "  memware backup             snapshot + transcript mirror to backup.dest\n"
            "  memware restore --latest   after a wipe (never re-backfill)"
        ),
    )
    s.add_argument("--dest", metavar="DIR", help="destination (default: backup.dest from config)")
    s.add_argument(
        "--keep",
        type=lambda v: [int(x) for x in v.replace(",", " ").split()],
        metavar="D1,D3,D7…",
        help="age buckets to keep (default 1,3,7,14)",
    )
    s.add_argument(
        "--transcripts",
        action="store_true",
        default=None,
        help="also mirror transcripts into <dest>/transcripts",
    )
    s.add_argument("--no-transcripts", dest="transcripts", action="store_false")
    s.add_argument("--transcript-src", metavar="DIR")
    s.add_argument(
        "--if-stale",
        type=float,
        metavar="HOURS",
        help="only back up if the newest snapshot is older than HOURS "
        "(safe to call from every session-end hook; no-op when no dest is set)",
    )
    s.add_argument("--quiet", action="store_true", help="print nothing on success")
    s.set_defaults(fn=cmd_backup)

    s = add("restore", "replace the store with a snapshot (the current store is saved aside first)")
    s.add_argument(
        "--from",
        dest="from_file",
        metavar="FILE",
        help="snapshot file (default: latest in backup.dest)",
    )
    s.add_argument("--dest", metavar="DIR", help="backup destination to pick the latest from")
    s.set_defaults(fn=cmd_restore)

    s = add(
        "setup",
        "guided one-time setup: index existing sessions, configure backups, choose whether "
        "derive runs automatically",
    )
    s.add_argument(
        "--yes",
        action="store_true",
        help="accept defaults; non-interactive (never switches derive on)",
    )
    s.set_defaults(fn=cmd_setup)

    s = add("config", "show or set configuration (e.g. backup.dest, backup.keep_days)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    s = add(
        "nuke",
        "permanently delete the store, config, and ALL backups (typed confirmation required)",
    )
    s.add_argument(
        "--confirm",
        metavar="PHRASE",
        help='must equal "DELETE ALL MEMWARE DATA" (else you are prompted)',
    )
    s.set_defaults(fn=cmd_nuke)

    s = add(
        "completions",
        "print a shell completion script (bash/zsh/fish)",
        epilog=(
            "Examples:\n"
            "  memware completions zsh  > ~/.zfunc/_memware\n"
            "  memware completions bash > ~/.local/share/bash-completion/completions/memware\n"
            "  memware completions fish > ~/.config/fish/completions/memware.fish"
        ),
    )
    s.add_argument("shell", choices=["bash", "zsh", "fish"])
    s.set_defaults(fn=cmd_completions)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if getattr(a, "ascii", False):
        os.environ["MEMWARE_ASCII"] = "1"  # honoured by memware.term for glyph fallback
    return int(a.fn(a))


if __name__ == "__main__":
    raise SystemExit(main())
