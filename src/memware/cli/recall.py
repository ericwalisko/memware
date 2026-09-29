"""`memware recall`."""

from __future__ import annotations

import argparse

from memware.cli._common import AddCommand, _emit
from memware.index import search_beliefs_multi, search_turns_multi
from memware.store import Store

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
    ("volatile", "volatile"),  # last: --plain column positions stay where scripts expect them
]


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
                "volatile": h.volatile,
                "snippet": h.snippet,
                "text": h.text if a.full else h.text[:300],
            }
            for h in hits
        ]
        _emit(a, rows, _RECALL_COLS)
    return 0


def register(add: AddCommand) -> None:
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
