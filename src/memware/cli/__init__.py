"""``memware`` command-line interface. Every command is also usable from a hook:
pass ``--from-hook`` to read the harness's JSON payload on stdin."""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
from pathlib import Path

from memware import __version__, relevance
from memware.cli import assertion as assertion_cmd
from memware.cli import backfill as backfill_cmd
from memware.cli import backup as backup_cmd
from memware.cli import beliefs as beliefs_cmd
from memware.cli import config as config_cmd
from memware.cli import context as context_cmd
from memware.cli import digest as digest_cmd
from memware.cli import exclude as exclude_cmd
from memware.cli import init as init_cmd
from memware.cli import notice as notice_cmd
from memware.cli import prune as prune_cmd
from memware.cli import read as read_cmd
from memware.cli import recall as recall_cmd
from memware.cli import restore as restore_cmd
from memware.cli import review as review_cmd
from memware.cli import scan as scan_cmd
from memware.cli import setup as setup_cmd
from memware.cli import stats as stats_cmd
from memware.cli import sync as sync_cmd
from memware.cli._common import (
    _HelpFormatter,
    _out,
)
from memware.derive import add_arguments as _derive_arguments
from memware.derive import cmd_derive

"""``capture.exclude`` hiding at least this share of the transcripts on disk is called out by
``memware exclude``, ``memware stats`` and ``memware backup``."""


"""Directory names Claude Code itself writes under a project directory."""


"""The value argparse stores for a text option given without its text: read it from --value-file,
a prompt that does not echo, or standard input."""


"""The key in the store's ``notice`` table that records the stale-belief notice was given."""


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

    stats_cmd.register(add)

    backup_cmd.register(add)

    restore_cmd.register(add)

    setup_cmd.register(add)

    config_cmd.register(add)

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
