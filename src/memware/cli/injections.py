"""`memware injections`."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from memware.cli._common import AddCommand
from memware.config import get_dotted, load_config
from memware.injections import scan
from memware.store import Store


def cmd_injections(a: argparse.Namespace) -> int:
    """What the prompt hook injected, read back from the transcripts: counts by default, one
    JSON line per injected fact with --jsonl. Writes nothing."""
    src = a.transcript_src or get_dotted(load_config(), "backup.transcript_src")
    src = str(src or "~/.claude/projects")
    since = None if a.days is None else datetime.now(UTC) - timedelta(days=a.days)
    with Store(a.db) as s:
        found, counts = scan(s.conn, src, since=since)
    if a.jsonl:
        for i in found:
            print(json.dumps(i.row(), ensure_ascii=False))
        return 0
    summary = counts.summary() | {
        "prompts": len({i.prompt_id for i in found}),
        "pairs": len({i.pair_id for i in found if i.pair_id}),
        "transcript_src": src,
    }
    print(json.dumps(summary, indent=2))
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "injections",
        "what the prompt hook injected, read back from the transcripts (for garden)",
        epilog=(
            "Counts only unless --jsonl, whose lines hold prompt and fact text. Interactive\n"
            "sessions only; what memware keeps out of its store is left out here too."
        ),
    )
    s.add_argument(
        "--days", type=float, default=None, metavar="N", help="only injections this recent"
    )
    s.add_argument(
        "--transcript-src",
        default=None,
        metavar="DIR",
        help="transcript tree to read (default: backup.transcript_src)",
    )
    s.add_argument(
        "--jsonl", action="store_true", help="one JSON line per injected fact, with its text"
    )
    s.set_defaults(fn=cmd_injections)
