"""`memware setup`: the guided first-run questions."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from memware import __version__
from memware.cli._common import AddCommand
from memware.ingest import sync_tree
from memware.store import Store


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


def register(add: AddCommand) -> None:
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
