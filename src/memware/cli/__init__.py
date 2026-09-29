"""``memware`` command-line interface. Every command is also usable from a hook:
pass ``--from-hook`` to read the harness's JSON payload on stdin."""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from memware import __version__, relevance
from memware.cli import assertion as assertion_cmd
from memware.cli import backfill as backfill_cmd
from memware.cli import beliefs as beliefs_cmd
from memware.cli import context as context_cmd
from memware.cli import digest as digest_cmd
from memware.cli import exclude as exclude_cmd
from memware.cli import init as init_cmd
from memware.cli import notice as notice_cmd
from memware.cli import prune as prune_cmd
from memware.cli import read as read_cmd
from memware.cli import recall as recall_cmd
from memware.cli import review as review_cmd
from memware.cli import scan as scan_cmd
from memware.cli import sync as sync_cmd
from memware.cli._common import (
    _count,
    _HelpFormatter,
    _hiding_verdict,
    _of,
    _out,
    _stale,
    _transcripts_on_disk,
)
from memware.cli.notice import _maybe_setup_hint
from memware.derive import add_arguments as _derive_arguments
from memware.derive import cmd_derive
from memware.derive import status as derive_status
from memware.digest import (
    injection_gate,
    resolve_project,
)
from memware.ingest import (
    sync_tree,
)
from memware.ledger import (
    orphaned_count,
    stale_turn_count,
)
from memware.store import Store
from memware.volatile import (
    REASONS,
    WINDOW_KEY,
    Gate,
    label,
    parse_days,
)

"""``capture.exclude`` hiding at least this share of the transcripts on disk is called out by
``memware exclude``, ``memware stats`` and ``memware backup``."""


"""Directory names Claude Code itself writes under a project directory."""


"""The value argparse stores for a text option given without its text: read it from --value-file,
a prompt that does not echo, or standard input."""


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
            if d["directory"].startswith((home + "/", home + os.sep))
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


"""The key in the store's ``notice`` table that records the stale-belief notice was given."""


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
    """Permanently delete the store, its config, review files, labeled prompts (``labels/``) AND
    every snapshot in the backup destination. Guarded by a typed confirmation so it cannot happen
    by accident."""
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
    labels = home / "labels"  # prompts labeled relevant or not, to calibrate injection
    print("This permanently deletes:")
    print(f"  store:      {a.db} (+ wal/shm)")
    print(f"  config:     {config_path()}")
    if labels.exists() or labels.is_symlink():
        print(f"  labels:     {labels}/ (labeled prompt text)")
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
    if labels.is_dir() and not labels.is_symlink():
        removed += sum(1 for p in labels.rglob("*") if not p.is_dir() or p.is_symlink())
        shutil.rmtree(labels)
    elif labels.exists() or labels.is_symlink():  # a link is removed, never followed
        labels.unlink()
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

    init_cmd.register(add)

    sync_cmd.register(add)

    backfill_cmd.register(add)

    exclude_cmd.register(add)

    recall_cmd.register(add)

    context_cmd.register(add)

    notice_cmd.register(add)

    digest_cmd.register(add)

    assertion_cmd.register(add)

    beliefs_cmd.register(add)

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

    read_cmd.register(add)

    review_cmd.register(add)

    prune_cmd.register(add)

    scan_cmd.register(add)

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


def _utf8_stdio() -> None:
    """Read and write pipes as UTF-8 on Windows, where Python otherwise uses the ANSI code page.
    Claude Code writes a hook's payload as UTF-8 and reads what a command prints as UTF-8, and
    the code page has no room for most of what a transcript says: a prompt would arrive garbled
    and the first arrow in a belief would crash the command. A console is unaffected."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        _utf8_stdio()
    a = build_parser().parse_args(argv)
    if getattr(a, "ascii", False):
        os.environ["MEMWARE_ASCII"] = "1"  # honoured by memware.term for glyph fallback
    return int(a.fn(a))
