"""``memware`` command-line interface. Every command is also usable from a hook:
pass ``--from-hook`` to read the harness's JSON payload on stdin."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from memware import __version__
from memware.index import (
    read_turns,
    search_beliefs,
    search_beliefs_multi,
    search_turns_multi,
)
from memware.ingest import capture_disabled, prune_sources, sync_file, sync_tree
from memware.ledger import Policy, approve, assert_belief, current, history, reject
from memware.review import HttpReviewBackend, JsonlReviewBackend, open_reviews, sync_reviews
from memware.store import DEFAULT_DB, Store


def _hook_payload() -> dict[str, object]:
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _out(obj: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    elif isinstance(obj, list):
        for row in obj:
            print(row if isinstance(row, str) else json.dumps(row, default=str))
    else:
        print(obj if isinstance(obj, str) else json.dumps(obj, default=str))


def cmd_init(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        _out({"db": str(s.path), **s.stats()}, a.json)
    return 0


def cmd_sync(a: argparse.Namespace) -> int:
    if a.from_hook and capture_disabled():
        return 0  # MEMWARE_NO_CAPTURE=1: this run must not enter the store
    paths = list(a.paths)
    if a.from_hook:
        tp = _hook_payload().get("transcript_path")
        if tp:
            paths.append(str(tp))
    if not paths:
        print("nothing to sync", file=sys.stderr)
        return 0
    with Store(a.db) as s:
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
    return 0


def cmd_backfill(a: argparse.Namespace) -> int:
    """One-time index of existing transcripts on a fresh machine.

    The plugin only captures new sessions; this reads what is already on disk.
    Idempotent — safe to re-run — and it honours the ignore-markers list.
    """
    root = Path(a.root).expanduser()
    if not root.exists():
        print(f"nothing to backfill: {root} does not exist", file=sys.stderr)
        return 0
    with Store(a.db) as s:
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
                "snippet": h.snippet,
                "text": h.text if a.full else h.text[:300],
            }
            for h in hits
        ]
        _out(rows, a.json)
    return 0


def cmd_context(a: argparse.Namespace) -> int:
    """Prompt-time helper: print currently valid beliefs relevant to the prompt."""
    prompt = a.prompt or str(_hook_payload().get("prompt", ""))
    if not prompt.strip():
        return 0
    with Store(a.db) as s:
        hits = search_beliefs(s, prompt, k=a.k, require_subject=True)
    if not hits:
        return 0
    lines = []
    for h in hits:
        value = h.text.removeprefix(f"{h.subject} {h.relation} ")
        since = f" (since {h.ts[:10]})" if h.ts else ""
        lines.append(f"- {h.subject} {h.relation}: {value}{since}")
    block = "Known facts (currently valid, from your memory ledger):\n" + "\n".join(lines)
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


def cmd_assert(a: argparse.Namespace) -> int:
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


def cmd_beliefs(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        rows = history(s, a.subject, a.relation) if a.relation else current(s, a.subject)
        _out(rows, a.json)
    return 0


def cmd_read(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        _out(read_turns(s, a.session, around=a.around, window=a.window), a.json)
    return 0


def cmd_review(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        if a.action == "list":
            _out([r.__dict__ for r in open_reviews(s)], a.json)
        elif a.action == "approve":
            _out(approve(s, a.id).__dict__, a.json)
        elif a.action == "reject":
            _out(reject(s, a.id).__dict__, a.json)
        elif a.action == "sync":
            backend: HttpReviewBackend | JsonlReviewBackend = (
                HttpReviewBackend(a.url, a.token)
                if a.url
                else JsonlReviewBackend(a.outbox, a.inbox)
            )
            _out(sync_reviews(s, backend), a.json)
    return 0


def cmd_prune(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        rep = prune_sources(s, glob=a.glob, containing=a.containing)
        _out({"sources_pruned": len(rep), "turns_removed": sum(rep.values())}, a.json)
    return 0


def cmd_stats(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        _out({"db": str(s.path), **s.stats()}, a.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memware", description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (env MEMWARE_DB)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return sp

    s = add("init", "create the database")
    s.set_defaults(fn=cmd_init)

    s = add("sync", "index new turns from transcripts")
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

    s = add("backfill", "one-time index of existing transcripts (run once on a new machine)")
    s.add_argument(
        "root",
        nargs="?",
        default="~/.claude/projects",
        help="transcript root (default: ~/.claude/projects)",
    )
    s.add_argument("--harness", default="claude-code")
    s.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    s.set_defaults(fn=cmd_backfill)

    s = add("recall", "search turns and beliefs; pass several phrasings to fuse them")
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

    s = add("context", "print valid beliefs relevant to a prompt (hook-friendly)")
    s.add_argument("prompt", nargs="?")
    s.add_argument("-k", type=int, default=6)
    s.add_argument("--from-hook", action="store_true")
    s.set_defaults(fn=cmd_context)

    s = add("assert", "record a belief; supersedes the previous value")
    s.add_argument("subject")
    s.add_argument("relation")
    s.add_argument("value")
    s.add_argument("--valid-from")
    s.add_argument("--source")
    s.add_argument("--reliability", type=float, default=0.5)
    s.add_argument(
        "--policy", choices=[x.value for x in Policy], default=Policy.GATE_CONFLICTS.value
    )
    s.set_defaults(fn=cmd_assert)

    s = add("beliefs", "current beliefs, or the history of one key")
    s.add_argument("subject", nargs="?")
    s.add_argument("relation", nargs="?")
    s.set_defaults(fn=cmd_beliefs)

    s = add("read", "read a session's turns")
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

    s = add("prune", "un-index sources by path glob and/or content marker")
    s.add_argument("--glob", metavar="GLOB")
    s.add_argument("--containing", metavar="TEXT")
    s.set_defaults(fn=cmd_prune)

    s = add("stats", "counts")
    s.set_defaults(fn=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":
    raise SystemExit(main())
