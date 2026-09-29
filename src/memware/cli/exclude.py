"""`memware exclude`: list, add or remove capture.exclude path globs."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from collections.abc import Collection
from dataclasses import asdict
from pathlib import Path
from typing import Any

from memware.cli._common import (
    AddCommand,
    _hiding_verdict,
    _of,
    _out,
    _plural,
    _print_blocks,
    _transcripts_on_disk,
)
from memware.cli.prune import _index_checked, _index_left_line, _scrub_notes, _scrubbed_line
from memware.digest import project_dir_name
from memware.ingest import Pruned
from memware.ledger import orphaned_count
from memware.store import Store


def _path_names(src: str) -> set[str]:
    """The names in a transcript source's path, as given and resolved."""
    root = Path(src).expanduser()
    return {*root.parts, *root.resolve().parts} - {"/"}


_LAYOUT_NAMES = ("subagents",)


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


def register(add: AddCommand) -> None:
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
