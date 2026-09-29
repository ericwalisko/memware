"""`memware init`."""

from __future__ import annotations

import argparse

from memware.cli._common import AddCommand, _out
from memware.cli.notice import _maybe_setup_hint
from memware.store import Store


def cmd_init(a: argparse.Namespace) -> int:
    _maybe_setup_hint(a)
    with Store(a.db) as s:
        _out({"db": str(s.path), **s.stats()}, a.json)
    return 0


def register(add: AddCommand) -> None:
    s = add("init", "create the database")
    s.set_defaults(fn=cmd_init)
