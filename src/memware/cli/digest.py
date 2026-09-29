"""`memware digest`."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from memware.cli._common import AddCommand, _hook_payload, _hook_store
from memware.digest import DEFAULT_MAX_CHARS, digest
from memware.store import Store


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


def register(add: AddCommand) -> None:
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
