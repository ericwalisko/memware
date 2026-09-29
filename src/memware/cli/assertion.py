"""`memware assert`."""

from __future__ import annotations

import argparse
import json
import sys

from memware.cli._common import AddCommand, _out
from memware.ledger import Policy, assert_belief
from memware.store import Store


def cmd_assert(a: argparse.Namespace) -> int:
    if a.subject == "-":
        return _assert_stdin(a)
    if a.relation is None or a.value is None:
        print(
            "assert needs SUBJECT RELATION VALUE, or `-` to read TSV lines from stdin",
            file=sys.stderr,
        )
        return 2
    with Store(a.db) as s:
        r = assert_belief(
            s,
            a.subject,
            a.relation,
            a.value,
            valid_from=a.valid_from,
            source=a.source,
            reliability=a.reliability,
            policy=Policy(a.policy),
        )
        _out(
            {
                "outcome": r.outcome.value,
                "belief_id": r.belief_id,
                "incumbent_id": r.incumbent_id,
                "review_id": r.review_id,
            },
            a.json,
        )
    return 0


def _assert_stdin(a: argparse.Namespace) -> int:
    """Batch-assert tab-separated ``subject<TAB>relation<TAB>value[<TAB>source]`` lines from
    stdin. Blank lines and lines starting with ``#`` are skipped. Pairs with ``beliefs --plain``
    so facts can round-trip through an editor or script: read them out, pipe them back in."""
    outcomes: list[dict[str, object]] = []
    with Store(a.db) as s:
        for raw in sys.stdin:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                print(f"skipped (need 3+ tab-separated fields): {line!r}", file=sys.stderr)
                continue
            source = parts[3] if len(parts) > 3 else a.source
            r = assert_belief(
                s,
                parts[0],
                parts[1],
                parts[2],
                valid_from=a.valid_from,
                source=source,
                reliability=a.reliability,
                policy=Policy(a.policy),
            )
            outcomes.append(
                {
                    "subject": parts[0],
                    "relation": parts[1],
                    "value": parts[2],
                    "outcome": r.outcome.value,
                    "belief_id": r.belief_id,
                }
            )
    if getattr(a, "json", False):
        print(json.dumps({"asserted": len(outcomes), "outcomes": outcomes}, indent=2, default=str))
    else:
        for o in outcomes:
            print(f"{o['outcome']}: {o['subject']} {o['relation']} = {o['value']}")
        print(f"({len(outcomes)} asserted)")
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "assert",
        "record a belief; supersedes the previous value",
        epilog=(
            "Examples:\n"
            '  memware assert api "listens on port" 8443 --source "session 3f2a"\n'
            "  printf 'api\\tlistens on port\\t8443\\n' | memware assert -   (batch TSV from stdin)"
        ),
    )
    s.add_argument("subject", help="subject, or `-` to batch-read TSV lines from stdin")
    s.add_argument("relation", nargs="?", help="relation (omit only when subject is `-`)")
    s.add_argument("value", nargs="?", help="value (omit only when subject is `-`)")
    s.add_argument("--valid-from")
    s.add_argument("--source")
    s.add_argument("--reliability", type=float, default=0.5)
    s.add_argument(
        "--policy", choices=[x.value for x in Policy], default=Policy.GATE_CONFLICTS.value
    )
    s.set_defaults(fn=cmd_assert)
