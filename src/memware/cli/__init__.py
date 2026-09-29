"""``memware`` command-line interface. Every command is also usable from a hook:
pass ``--from-hook`` to read the harness's JSON payload on stdin."""

from __future__ import annotations

import argparse
import io
import os
import sys

from memware import __version__
from memware.cli import assertion as assertion_cmd
from memware.cli import backfill as backfill_cmd
from memware.cli import backup as backup_cmd
from memware.cli import beliefs as beliefs_cmd
from memware.cli import completions as completions_cmd
from memware.cli import config as config_cmd
from memware.cli import context as context_cmd
from memware.cli import derive as derive_cmd
from memware.cli import digest as digest_cmd
from memware.cli import exclude as exclude_cmd
from memware.cli import init as init_cmd
from memware.cli import notice as notice_cmd
from memware.cli import nuke as nuke_cmd
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
)

"""``capture.exclude`` hiding at least this share of the transcripts on disk is called out by
``memware exclude``, ``memware stats`` and ``memware backup``."""


"""Directory names Claude Code itself writes under a project directory."""


"""The value argparse stores for a text option given without its text: read it from --value-file,
a prompt that does not echo, or standard input."""


"""The key in the store's ``notice`` table that records the stale-belief notice was given."""


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

    derive_cmd.register(add)

    read_cmd.register(add)

    review_cmd.register(add)

    prune_cmd.register(add)

    scan_cmd.register(add)

    stats_cmd.register(add)

    backup_cmd.register(add)

    restore_cmd.register(add)

    setup_cmd.register(add)

    config_cmd.register(add)

    nuke_cmd.register(add)

    completions_cmd.register(add)

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
