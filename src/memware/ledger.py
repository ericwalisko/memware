"""The belief ledger: deterministic, bi-temporal supersession.

Every belief is a ``(subject, relation) -> value`` triple with a validity
interval ``[valid_from, valid_to)`` in *event time* (when it became true, i.e.
the timestamp of the evidence) and a ``recorded_at`` in *transaction time*.

The rule is deliberately simple and needs no model:

* same key, same value        -> reinforce (reliability rises, use is counted)
* same key, newer value       -> supersede: the incumbent gets ``valid_to``
* same key, older value       -> filed as history; the timeline stays consistent
* a less reliable challenger  -> parked as a candidate and sent to review

Because ordering is decided by ``valid_from`` and not by insertion order, the
end state is the same however the evidence arrives — a backfill can run in
any order, twice, or in batches.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from memware.store import Store, now_iso

_WS = re.compile(r"\s+")
_EDGE = re.compile(r"^[\s\"'`.,;:!?()\[\]{}]+|[\s\"'`.,;:!?()\[\]{}]+$")


def normalize(text: str) -> str:
    """Canonical form used for keys and value comparison."""
    return _WS.sub(" ", _EDGE.sub("", text.strip().lower()))


def make_key(subject: str, relation: str) -> str:
    return f"{normalize(subject)}|{normalize(relation)}"


class Policy(StrEnum):
    """What happens when a newer value challenges a committed belief."""

    AUTO = "auto"
    GATE_CONFLICTS = "gate_conflicts"
    AWAIT_CONFIRMATION = "await_confirmation"


class Outcome(StrEnum):
    CREATED = "created"
    REINFORCED = "reinforced"
    SUPERSEDED = "superseded"
    HISTORICAL = "historical"
    PENDING_REVIEW = "pending_review"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    belief_id: int
    incumbent_id: int | None = None
    review_id: int | None = None


def _current(store: Store, key: str) -> sqlite3.Row | None:
    row = store.conn.execute(
        "SELECT * FROM belief WHERE key=? AND valid_to IS NULL AND status='committed' "
        "ORDER BY valid_from DESC LIMIT 1",
        (key,),
    ).fetchone()
    return row  # type: ignore[no-any-return]


def _insert(
    store: Store,
    *,
    key: str,
    subject: str,
    relation: str,
    value: str,
    valid_from: str,
    valid_to: str | None,
    status: str,
    reliability: float,
    source: str | None,
) -> int:
    cur = store.conn.execute(
        "INSERT INTO belief(key,subject,relation,value,valid_from,valid_to,recorded_at,"
        "status,reliability,source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            key,
            subject,
            relation,
            value,
            valid_from,
            valid_to,
            now_iso(),
            status,
            reliability,
            source,
        ),
    )
    return int(cur.lastrowid or 0)


def assert_belief(
    store: Store,
    subject: str,
    relation: str,
    value: str,
    *,
    valid_from: str | None = None,
    source: str | None = None,
    reliability: float = 0.5,
    policy: Policy = Policy.GATE_CONFLICTS,
) -> Result:
    """Record that ``subject relation value`` held from ``valid_from``.

    ``valid_from`` is ISO-8601 event time; it defaults to now. ``reliability``
    is in [0, 1] — use higher values for facts a human stated or that were
    verified against a source, lower for facts an agent inferred.
    """
    if not (0.0 <= reliability <= 1.0):
        raise ValueError("reliability must be within [0, 1]")
    key = make_key(subject, relation)
    vf = valid_from or now_iso()
    nv = normalize(value)
    conn = store.conn

    conn.execute("BEGIN IMMEDIATE")
    try:
        incumbent = _current(store, key)

        # 1. Same value as the incumbent: reinforce, never duplicate.
        if incumbent is not None and normalize(incumbent["value"]) == nv:
            conn.execute(
                "UPDATE belief SET reliability=MAX(reliability, ?), use_count=use_count+1, "
                "valid_from=MIN(valid_from, ?) WHERE id=?",
                (reliability, vf, incumbent["id"]),
            )
            return Result(Outcome.REINFORCED, int(incumbent["id"]))

        # 2. Older evidence arriving late: file it into the timeline as history.
        if incumbent is not None and vf < incumbent["valid_from"]:
            successor = conn.execute(
                "SELECT id, valid_from FROM belief WHERE key=? AND valid_from>? "
                "AND status!='rejected' ORDER BY valid_from ASC LIMIT 1",
                (key, vf),
            ).fetchone()
            predecessor = conn.execute(
                "SELECT id, value, valid_to FROM belief WHERE key=? AND valid_from<=? "
                "AND status!='rejected' ORDER BY valid_from DESC LIMIT 1",
                (key, vf),
            ).fetchone()
            if predecessor is not None and normalize(predecessor["value"]) == nv:
                conn.execute(
                    "UPDATE belief SET use_count=use_count+1 WHERE id=?", (predecessor["id"],)
                )
                return Result(Outcome.REINFORCED, int(predecessor["id"]))
            bid = _insert(
                store,
                key=key,
                subject=subject,
                relation=relation,
                value=value,
                valid_from=vf,
                valid_to=successor["valid_from"] if successor else None,
                status="committed",
                reliability=reliability,
                source=source,
            )
            if predecessor is not None and (
                predecessor["valid_to"] is None or predecessor["valid_to"] > vf
            ):
                conn.execute(
                    "UPDATE belief SET valid_to=?, superseded_by=? WHERE id=?",
                    (vf, bid, predecessor["id"]),
                )
            if successor is not None:
                conn.execute("UPDATE belief SET superseded_by=? WHERE id=?", (successor["id"], bid))
            return Result(Outcome.HISTORICAL, bid, int(incumbent["id"]))

        # 3. Brand-new key: commit.
        if incumbent is None:
            bid = _insert(
                store,
                key=key,
                subject=subject,
                relation=relation,
                value=value,
                valid_from=vf,
                valid_to=None,
                status="committed",
                reliability=reliability,
                source=source,
            )
            return Result(Outcome.CREATED, bid)

        # 4. Newer, different value: supersede or park for review.
        weaker = reliability < float(incumbent["reliability"])
        gate = policy is Policy.AWAIT_CONFIRMATION or (policy is Policy.GATE_CONFLICTS and weaker)
        if gate:
            bid = _insert(
                store,
                key=key,
                subject=subject,
                relation=relation,
                value=value,
                valid_from=vf,
                valid_to=None,
                status="candidate",
                reliability=reliability,
                source=source,
            )
            reason = (
                "challenges committed belief with lower reliability"
                if weaker
                else "policy requires confirmation"
            )
            cur = conn.execute(
                "INSERT INTO review(belief_id,incumbent_id,reason,opened_at) VALUES (?,?,?,?)",
                (bid, incumbent["id"], reason, now_iso()),
            )
            return Result(
                Outcome.PENDING_REVIEW, bid, int(incumbent["id"]), int(cur.lastrowid or 0)
            )

        bid = _insert(
            store,
            key=key,
            subject=subject,
            relation=relation,
            value=value,
            valid_from=vf,
            valid_to=None,
            status="committed",
            reliability=reliability,
            source=source,
        )
        conn.execute(
            "UPDATE belief SET valid_to=?, superseded_by=? WHERE id=?", (vf, bid, incumbent["id"])
        )
        return Result(Outcome.SUPERSEDED, bid, int(incumbent["id"]))
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        pass
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")


def approve(store: Store, review_id: int) -> Result:
    """Accept a candidate: it becomes the current belief, the incumbent is retired."""
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        rv = conn.execute(
            "SELECT * FROM review WHERE id=? AND decision IS NULL", (review_id,)
        ).fetchone()
        if rv is None:
            raise LookupError(f"no open review {review_id}")
        cand = conn.execute("SELECT * FROM belief WHERE id=?", (rv["belief_id"],)).fetchone()
        incumbent = _current(store, cand["key"])
        if incumbent is not None and incumbent["id"] != cand["id"]:
            conn.execute(
                "UPDATE belief SET valid_to=?, superseded_by=? WHERE id=?",
                (cand["valid_from"], cand["id"], incumbent["id"]),
            )
        conn.execute("UPDATE belief SET status='committed' WHERE id=?", (cand["id"],))
        conn.execute(
            "UPDATE review SET decision='approved', decided_at=? WHERE id=?",
            (now_iso(), review_id),
        )
        return Result(
            Outcome.SUPERSEDED,
            int(cand["id"]),
            int(incumbent["id"]) if incumbent else None,
            review_id,
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")


def reject(store: Store, review_id: int) -> Result:
    """Refuse a candidate: it is closed at its own start and never surfaces."""
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        rv = conn.execute(
            "SELECT * FROM review WHERE id=? AND decision IS NULL", (review_id,)
        ).fetchone()
        if rv is None:
            raise LookupError(f"no open review {review_id}")
        conn.execute(
            "UPDATE belief SET status='rejected', valid_to=valid_from WHERE id=?",
            (rv["belief_id"],),
        )
        conn.execute(
            "UPDATE review SET decision='rejected', decided_at=? WHERE id=?",
            (now_iso(), review_id),
        )
        return Result(
            Outcome.REINFORCED, int(rv["incumbent_id"] or rv["belief_id"]), None, review_id
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")


def current(store: Store, subject: str | None = None) -> list[dict[str, object]]:
    """Currently valid, committed beliefs — the only ones that should reach a prompt."""
    sql = "SELECT * FROM belief WHERE valid_to IS NULL AND status='committed'"
    args: tuple[object, ...] = ()
    if subject is not None:
        sql += " AND key LIKE ?"
        args = (f"{normalize(subject)}|%",)
    sql += " ORDER BY subject, relation"
    return [dict(r) for r in store.conn.execute(sql, args)]


def history(store: Store, subject: str, relation: str) -> list[dict[str, object]]:
    """The full timeline for one key, oldest first, rejected candidates excluded."""
    rows = store.conn.execute(
        "SELECT * FROM belief WHERE key=? AND status!='rejected' ORDER BY valid_from, id",
        (make_key(subject, relation),),
    )
    return [dict(r) for r in rows]


def touch(store: Store, belief_ids: list[int]) -> None:
    """Record a retrieval (the testing effect): used beliefs rank higher later."""
    if not belief_ids:
        return
    ts = now_iso()
    store.conn.executemany(
        "UPDATE belief SET use_count=use_count+1, last_used=? WHERE id=?",
        [(ts, i) for i in belief_ids],
    )
