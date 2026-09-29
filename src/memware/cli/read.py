"""`memware read`."""

from __future__ import annotations

import argparse

from memware.cli._common import AddCommand, _emit
from memware.index import read_turns
from memware.store import Store

_TURN_COLS = [("id", "id"), ("seq", "seq"), ("role", "role"), ("ts", "when"), ("text", "text")]


def cmd_read(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        _emit(a, read_turns(s, a.session, around=a.around, window=a.window), _TURN_COLS)
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "read",
        "read a session's turns",
        epilog="Example:\n  memware read <session-id> --around <turn-id> --window 5",
    )
    s.add_argument("session")
    s.add_argument("--around", type=int)
    s.add_argument("--window", type=int, default=5)
    s.set_defaults(fn=cmd_read)
