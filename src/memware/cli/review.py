"""`memware review`."""

from __future__ import annotations

import argparse
import sys

from memware.cli._common import AddCommand, _out
from memware.ledger import approve, reject
from memware.review import HttpReviewBackend, JsonlReviewBackend, open_reviews, sync_reviews
from memware.store import Store


def cmd_review(a: argparse.Namespace) -> int:
    with Store(a.db) as s:
        if a.action == "list":
            _out([r.__dict__ for r in open_reviews(s)], a.json)
        elif a.action in ("approve", "reject"):
            try:
                result = (approve if a.action == "approve" else reject)(s, a.id)
            except LookupError as e:  # no open review, or a candidate that cannot be approved
                print(str(e).strip("'\""), file=sys.stderr)
                return 2
            _out(result.__dict__, a.json)
        elif a.action == "sync":
            backend: HttpReviewBackend | JsonlReviewBackend = (
                HttpReviewBackend(a.url, a.token)
                if a.url
                else JsonlReviewBackend(a.outbox, a.inbox)
            )
            _out(sync_reviews(s, backend), a.json)
    return 0


def register(add: AddCommand) -> None:
    s = add("review", "list/approve/reject/sync contested supersessions")
    s.add_argument("action", choices=["list", "approve", "reject", "sync"])
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--outbox", default="~/.memware/review-outbox.jsonl")
    s.add_argument("--inbox", default="~/.memware/review-inbox.jsonl")
    s.add_argument("--url")
    s.add_argument("--token")
    s.set_defaults(fn=cmd_review)
