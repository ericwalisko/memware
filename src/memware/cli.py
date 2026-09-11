"""``memware`` command-line interface. Every command is also usable from a hook:
pass ``--from-hook`` to read the harness's JSON payload on stdin."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from memware import __version__
from memware.derive import add_arguments as _derive_arguments
from memware.derive import cmd_derive
from memware.derive import status as derive_status
from memware.digest import DEFAULT_MAX_CHARS, digest
from memware.index import (
    read_turns,
    search_beliefs,
    search_beliefs_multi,
    search_turns_multi,
)
from memware.ingest import capture_disabled, prune_sources, prune_turns, sync_file, sync_tree
from memware.ledger import Policy, approve, assert_belief, current, history, reject
from memware.review import HttpReviewBackend, JsonlReviewBackend, open_reviews, sync_reviews
from memware.store import Store


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
    _maybe_setup_hint(a)
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
        _emit(a, rows, _RECALL_COLS)
    return 0


def cmd_context(a: argparse.Namespace) -> int:
    """Prompt-time helper: print currently valid beliefs relevant to the prompt."""
    prompt = a.prompt or str(_hook_payload().get("prompt", ""))
    if not prompt.strip():
        return 0
    with Store(a.db) as s:
        # Injection is not retrieval: nobody asked for these, so they must not gain activation
        # or count as used — use_count means an agent or a person retrieved the belief.
        hits = search_beliefs(s, prompt, k=a.k, require_subject=True, record_use=False)
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


def cmd_digest(a: argparse.Namespace) -> int:
    """Session-start helper: what memware holds for this project (see memware.digest)."""
    payload = _hook_payload() if a.from_hook else {}
    cwd = a.cwd or payload.get("cwd") or os.getcwd()
    if a.db != ":memory:" and not Path(a.db).expanduser().exists():
        return 0  # no store yet: nothing to say, and a hook must not create one
    session, transcript = payload.get("session_id"), payload.get("transcript_path")
    with Store(a.db) as s:
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


def cmd_beliefs(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
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
        if a.turns_containing:
            removed = prune_turns(s, containing=a.turns_containing)
            _out({"turns_removed": removed}, a.json)
        else:
            rep = prune_sources(s, glob=a.glob, containing=a.containing)
            _out({"sources_pruned": len(rep), "turns_removed": sum(rep.values())}, a.json)
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
    return out


def _when(ts: str | None, age: float | None, never: str) -> str:
    if not ts:
        return never
    if age is None:
        return ts
    ago = f"{age:.1f} hours" if age < 48 else f"{int(age // 24)} days"
    return f"{ts} ({ago} ago)"


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
        [
            ("derive auto", "on" if d["auto"] else "off"),
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
        ],
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


def cmd_stats(a: argparse.Namespace) -> int:
    _maybe_setup_hint(a)
    with Store(a.db) as s:
        report: dict[str, Any] = {"db": str(s.path), **s.stats()}
        report["derive"] = derive_status(s.conn, s.path)
        report["utilization"] = s.utilization()
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


def _release(version: str) -> tuple[int, ...]:
    m = re.match(r"\d+(?:\.\d+)*", version.strip())
    return tuple(int(x) for x in m.group().split(".")) if m else ()


def _older(version: str, than: str) -> bool:
    """Whether ``version`` is older than ``than``, compared as versions, never as strings
    (as strings, "0.10.0" sorts before "0.4.0")."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # memware has no runtime dependencies; packaging is usually there
        pass
    else:
        try:
            return Version(version) < Version(than)
        except InvalidVersion:
            pass
    a, b = _release(version), _release(than)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


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


def cmd_notice(a: argparse.Namespace) -> int:
    """The consent hints, for someone who only uses the plugin: they never type a memware command,
    so they never see what `stats` prints. The plugin runs this in the foreground at session start,
    and Claude Code shows the ``systemMessage`` to them. It reads the config file and nothing else
    (never the store), so it is quick, and it cannot fail a session start: whatever goes wrong, it
    prints nothing and exits 0."""
    try:
        from memware.config import config_path, get_dotted

        if a.from_hook and _hook_payload().get("source") == "compact":
            return 0  # a compaction mid-session, not a session the person just opened
        # No file means nobody has answered. A file that will not parse is not an answer either,
        # but it may hold one: stay quiet rather than ask at every session start.
        path = config_path()
        user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(user, dict):
            return 0
        hints = _consent_hints(get_dotted(user, "setup.completed_version"))
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
            bk.mirror_transcripts(get_dotted(cfg, "backup.transcript_src") or src_default, dpath)
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
    print("  • Sensitive session? Set MEMWARE_NO_CAPTURE=1 and it is never indexed.")
    print("  • After a wipe, `memware restore --latest` — never wipe-and-re-backfill (backfill")
    print("    only re-indexes transcripts still on disk). See docs/backup.md.")
    return 0


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
    for name in ("ignore-markers.txt", "review-outbox.jsonl", "review-inbox.jsonl"):
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
    "ledger and transcript index. No daemon, no vector database, no model in the loop."
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
  MEMWARE_NO_CAPTURE=1  never index the current session
  NO_COLOR            honoured by construction — memware emits no colour at all

Files:
  <home>/config.json          configuration (see `memware config`)
  <home>/ignore-markers.txt   content signatures never to index

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

    s = add("context", "print valid beliefs relevant to a prompt (hook-friendly)")
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
            '  memware beliefs api "listens on port"  full history of one key'
        ),
    )
    s.add_argument("subject", nargs="?")
    s.add_argument("relation", nargs="?")
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
        "un-index whole sources (--glob/--containing) or individual boilerplate turns (--turns-containing)",
    )
    s.add_argument("--glob", metavar="GLOB", help="un-index sources whose path matches this glob")
    s.add_argument(
        "--containing", metavar="TEXT", help="un-index whole sources whose head contains TEXT"
    )
    s.add_argument(
        "--turns-containing",
        metavar="TEXT",
        help="delete individual turns starting with TEXT (keeps the rest of each session)",
    )
    s.set_defaults(fn=cmd_prune)

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
