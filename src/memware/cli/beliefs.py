"""`memware beliefs`: current beliefs, one key's history, explain, retract."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from typing import Any

from memware.cli._common import AddCommand, _emit, _gate, _print_blocks, _stale
from memware.cli.prune import _cascade
from memware.derive import open_readonly
from memware.ledger import confirmed_sql, current, history, retract
from memware.store import Store
from memware.volatile import DERIVED_RELIABILITY, DERIVED_SOURCE, Explanation, label

_BELIEF_COLS = [
    ("id", "id"),
    ("subject", "subject"),
    ("relation", "relation"),
    ("value", "value"),
    ("valid_from", "valid from"),
    ("valid_to", "valid to"),
    ("reliability", "reliability"),
    ("status", "status"),
    ("source", "source"),
    ("volatile", "volatile"),  # last: --plain column positions stay where scripts expect them
]


_STALE_COLS = [
    ("id", "id"),
    ("subject", "subject"),
    ("relation", "relation"),
    ("value", "value"),
    ("valid_from", "valid from"),
    ("reason", "left out"),
    ("why", "why"),
    ("source", "source"),
]


def _retract_ids(a: argparse.Namespace) -> list[int] | None:
    """The belief ids after `beliefs retract`, or None when the words there are not all ids (a
    key whose subject is "retract" stays readable as history)."""
    words = [w for w in (a.relation, *a.ids) if w is not None]
    return [int(w) for w in words] if all(w.isdigit() for w in words) else None


_CHECK_LABELS = {
    "reliability": "reliability above 0.5",
    "source": "not a session pointer",
    "confirmed": "confirmed by a person",
    "manifest": "manifest overrules it",
    "window": "inside the window",
}


def _explained(row: dict[str, Any], e: Explanation, ledger: str) -> dict[str, Any]:
    """One belief's ``--explain`` record: what the gate found, test by test."""
    current = ledger in ("", "committed, current")
    why = e.why if current else f"not current ({ledger}), so never injected; if it were, {e.why}"
    return {
        **({"id": row["id"]} if "id" in row else {}),
        "subject": row["subject"],
        "relation": row["relation"],
        "value": row["value"],
        "in_ledger": ledger or None,
        "class": e.decision.cls or "durable",
        "injected": e.injected and current,
        "why": why,
        "left_out": None if e.verdict is None else asdict(e.verdict),
        "tests": [
            {
                "class": t.cls,
                "fired": t.fired,
                "because": t.because,
                "veto": None if t.veto is None else t.veto._asdict(),
            }
            for t in e.decision.tests
        ],
        "checks": [c._asdict() for c in e.checks],
    }


def _found(flag: bool, detail: str) -> str:
    return f"{'yes' if flag else 'no'}: {detail}"


def _print_explained(r: dict[str, Any]) -> None:
    head: list[tuple[str, str]] = [
        (k, str(r[k])) for k in ("id", "subject", "relation", "value") if k in r
    ]
    head.append(
        ("in ledger", r["in_ledger"] or "no; judged as derive would file it (reliability 0.5)")
    )
    head += [
        ("class", r["class"]),
        ("injected", "yes" if r["injected"] else "no"),
        ("why", r["why"]),
    ]
    tests = [(f"is a {label(t['class'])}", _found(t["fired"], t["because"])) for t in r["tests"]]
    checks = [(_CHECK_LABELS[c["name"]], _found(c["applies"], c["detail"])) for c in r["checks"]]
    _print_blocks([head, tests, checks])


def _belief_row(db: str, belief_id: int) -> dict[str, Any] | None:
    """The belief with this id, whatever its status, through a handle that cannot write."""
    conn = open_readonly(db)
    try:
        try:
            got = conn.execute(
                f"SELECT *, {confirmed_sql()} FROM belief WHERE id=?", (belief_id,)
            ).fetchone()
        except sqlite3.OperationalError:  # a store older than the confirmation table
            got = conn.execute(
                "SELECT *, 0 AS confirmed FROM belief WHERE id=?", (belief_id,)
            ).fetchone()
        return None if got is None else dict(got)
    finally:
        conn.close()


def _ledger_state(row: dict[str, Any]) -> str:
    if row["status"] != "committed":
        return str(row["status"])
    if row["valid_to"] is not None:
        by = f" by {row['superseded_by']}" if row["superseded_by"] is not None else ""
        return f"superseded{by} at {row['valid_to']}"
    return "committed, current"


def _beliefs_explain(a: argparse.Namespace) -> int:
    """``beliefs --explain``: why one belief is or is not injected. Reads only; a triple given
    with --subject, --relation and --value opens no store at all."""
    triple = (a.explain_subject, a.explain_relation, a.explain_value)
    ref = a.explain or (a.subject if a.subject and a.subject.isdigit() else "")
    extra = a.stale or a.orphaned or a.apply or a.relation or a.ids or a.subject not in (None, ref)
    if extra or (ref and any(x is not None for x in triple)):
        print(
            "--explain takes one belief id, or --subject, --relation and --value", file=sys.stderr
        )
        return 2
    if ref:
        if not ref.isdigit():
            print(f"--explain takes a belief id, not {ref!r}", file=sys.stderr)
            return 2
        try:
            got = _belief_row(a.db, int(ref))
        except (OSError, sqlite3.Error) as err:
            print(f"cannot read the store: {err}", file=sys.stderr)
            return 1
        if got is None:
            print(f"no belief {ref}", file=sys.stderr)
            return 1
        row, ledger = got, _ledger_state(got)
    elif all(x is not None for x in triple):
        subject, relation, value = (str(x) for x in triple)
        row = {
            "subject": subject,
            "relation": relation,
            "value": value,
            "valid_from": None,
            "reliability": DERIVED_RELIABILITY,
            "source": DERIVED_SOURCE,
        }
        ledger = ""
    else:
        print(
            "--explain takes one belief id, or all of --subject, --relation and --value",
            file=sys.stderr,
        )
        return 2
    record = _explained(row, _gate(a).explain(row), ledger)
    if getattr(a, "json", False):
        print(json.dumps(record, indent=2, default=str))
    else:
        _print_explained(record)
    return 0


def cmd_beliefs(a: argparse.Namespace) -> int:
    if a.explain is not None or any(
        x is not None for x in (a.explain_subject, a.explain_relation, a.explain_value)
    ):
        if a.explain is None:
            print("--subject, --relation and --value go with --explain", file=sys.stderr)
            return 2
        return _beliefs_explain(a)
    if a.subject == "retract" and _retract_ids(a) is not None:
        return _beliefs_retract(a)
    if a.orphaned or a.apply:
        print("--orphaned and --apply belong to `memware beliefs retract`", file=sys.stderr)
        return 2
    if a.ids:
        print(f"unexpected arguments: {' '.join(a.ids)}", file=sys.stderr)
        return 2
    with Store(a.db) as s:
        if a.stale:
            if a.relation:
                print("--stale takes a subject, not a key", file=sys.stderr)
                return 2
            _emit(a, _stale(s, _gate(a), a.subject), _STALE_COLS)
            return 0
        rows = history(s, a.subject, a.relation) if a.relation else current(s, a.subject)
        _emit(a, rows, _BELIEF_COLS)
    return 0


def _not_retractable(s: Store, ids: list[int]) -> list[str]:
    """Why each of ``ids`` cannot be retracted by id: no committed belief, or one that is no
    longer current. A superseded belief reaches no prompt already, and retracting it would move
    the end of its interval, which is history."""
    marks = ",".join("?" * len(ids))
    rows = {
        r["id"]: r
        for r in s.conn.execute(
            f"SELECT id, status, valid_to, superseded_by FROM belief WHERE id IN ({marks})", ids
        )
    }
    out = []
    for i in ids:
        r = rows.get(i)
        if r is None or r["status"] != "committed":
            out.append(f"no committed belief with id {i}")
        elif r["valid_to"] is not None:
            by = f" by #{r['superseded_by']}" if r["superseded_by"] else ""
            out.append(
                f"belief {i} is not current: it was superseded{by} at {r['valid_to']}, and "
                "retract by id only acts on current beliefs"
            )
    return out


def _beliefs_retract(a: argparse.Namespace) -> int:
    ids = _retract_ids(a) or []
    if a.orphaned + a.stale + bool(ids) != 1:
        print(
            "beliefs retract needs --orphaned (beliefs whose cited session is no longer indexed), "
            "--stale (beliefs injection leaves out) or belief ids, and takes one of them",
            file=sys.stderr,
        )
        return 2
    with Store(a.db) as s:
        if a.orphaned:
            plan = retract(s, reason="memware beliefs retract --orphaned", apply=a.apply)
            _cascade(a, plan, a.apply)
            return 0
        if a.stale:
            chosen = {
                r["id"]: f"left out of injection, {label(r['reason'])}: {r['why']} "
                "(memware beliefs retract --stale)"
                for r in _stale(s, _gate(a))
            }
        else:
            why = f"retracted by id (memware beliefs retract {' '.join(map(str, ids))})"
            chosen = dict.fromkeys(ids, why)
            refused = _not_retractable(s, ids)
            if refused:
                print("\n".join([*refused, "nothing written"]), file=sys.stderr)
                return 2
        plan = retract(s, reason="", apply=a.apply, beliefs=chosen)
    _cascade(a, plan, a.apply, by_session=False)
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "beliefs",
        "current beliefs, or the history of one key",
        epilog=(
            "Examples:\n"
            "  memware beliefs                        all current beliefs\n"
            "  memware beliefs api                    current beliefs about a subject\n"
            '  memware beliefs api "listens on port"  full history of one key\n'
            "  memware beliefs --stale                what injection leaves out, and why\n"
            "  memware beliefs --explain 12           why belief 12 is or is not injected\n"
            "  memware beliefs --explain --subject api --relation 'error rate' --value 2%\n"
            "                                         the same for a triple, with no store\n"
            "  memware beliefs retract --orphaned     dry run: beliefs whose session is gone\n"
            "  memware beliefs retract --orphaned --apply   retract them (rows are kept)\n"
            "  memware beliefs retract --stale --apply      retract what --stale lists\n"
            "  memware beliefs retract 12 15 --apply        retract beliefs by id"
        ),
    )
    s.add_argument(
        "subject", nargs="?", help="a subject, or `retract` (with --orphaned, --stale or ids)"
    )
    s.add_argument("relation", nargs="?")
    s.add_argument("ids", nargs="*", help=argparse.SUPPRESS)
    s.add_argument(
        "--orphaned",
        action="store_true",
        help="with `retract`: committed beliefs citing a session that is no longer indexed",
    )
    s.add_argument(
        "--stale",
        action="store_true",
        help="current beliefs the prompt hook and digest leave out: a derived measurement, "
        "moving version or status, or a version the project manifest overrules; with `retract`, "
        "retract them",
    )
    s.add_argument(
        "--explain",
        nargs="?",
        const="",
        metavar="ID",
        help="why one belief is or is not injected: its class or durable, the test that decided "
        "it, the exemptions and the manifest check; read-only",
    )
    s.add_argument(
        "--subject",
        dest="explain_subject",
        metavar="S",
        help="with --explain, --relation and --value: judge a triple not in the ledger, with no "
        "store",
    )
    s.add_argument(
        "--relation", dest="explain_relation", metavar="R", help="with --explain: the relation"
    )
    s.add_argument("--value", dest="explain_value", metavar="V", help="with --explain: the value")
    s.add_argument(
        "--cwd",
        metavar="DIR",
        help="with --stale or --explain: the project whose manifest version is checked "
        "(default: this one)",
    )
    s.add_argument(
        "--apply",
        action="store_true",
        help="with `retract`: write the retraction; without it nothing is written",
    )
    s.set_defaults(fn=cmd_beliefs)
