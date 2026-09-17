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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from memware.store import Store, now_iso
from memware.volatile import human_stated, volatility

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


def confirmed_sql(alias: str = "belief") -> str:
    """A select-list column, ``confirmed``: 1 when a person confirmed the belief (see
    :func:`_confirm`). The injection gate reads it to treat that belief as human-stated."""
    return f"EXISTS (SELECT 1 FROM confirmation c WHERE c.belief_id = {alias}.id) AS confirmed"


def _confirm(store: Store, row: sqlite3.Row, source: str | None) -> None:
    """Record that a person stated a belief derive wrote. The belief row is not touched: its
    source still says where derive read it."""
    if not human_stated(row["reliability"], row["source"]):
        store.conn.execute(
            "INSERT OR IGNORE INTO confirmation(belief_id, confirmed_at, source) VALUES (?,?,?)",
            (row["id"], now_iso(), source),
        )


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

        # 1. Same value as the incumbent: reinforce, never duplicate. A person restating what
        # derive wrote confirms it, which is how they keep a fact the injection gate left out.
        if incumbent is not None and normalize(incumbent["value"]) == nv:
            conn.execute(
                "UPDATE belief SET reliability=MAX(reliability, ?), use_count=use_count+1, "
                "valid_from=MIN(valid_from, ?) WHERE id=?",
                (reliability, vf, incumbent["id"]),
            )
            if human_stated(reliability, source):
                _confirm(store, incumbent, source)
            return Result(Outcome.REINFORCED, int(incumbent["id"]))

        # 2. Older evidence arriving late: file it into the timeline as history.
        if incumbent is not None and vf < incumbent["valid_from"]:
            successor = conn.execute(
                "SELECT id, valid_from FROM belief WHERE key=? AND valid_from>? "
                "AND status NOT IN ('rejected','retracted') ORDER BY valid_from ASC LIMIT 1",
                (key, vf),
            ).fetchone()
            predecessor = conn.execute(
                "SELECT id, value, valid_to, reliability, source FROM belief WHERE key=? "
                "AND valid_from<=? "
                "AND status NOT IN ('rejected','retracted') ORDER BY valid_from DESC LIMIT 1",
                (key, vf),
            ).fetchone()
            if predecessor is not None and normalize(predecessor["value"]) == nv:
                conn.execute(
                    "UPDATE belief SET use_count=use_count+1 WHERE id=?", (predecessor["id"],)
                )
                if human_stated(reliability, source):
                    _confirm(store, predecessor, source)
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
    """Accept a candidate: it becomes the current belief, the incumbent is retired. A person
    decided, so a derived candidate counts as confirmed (see :func:`_confirm`)."""
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
        _confirm(store, cand, f"review #{review_id} approved")
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
    """Current, committed beliefs: no superseded or retracted value is among them. Each row
    carries ``confirmed`` (a person confirmed a derived belief) and ``volatile``, the class of a
    derived belief that was a measurement, a moving version or a status when recorded
    (:mod:`memware.volatile`), which no unsolicited injection includes."""
    sql = f"SELECT *, {confirmed_sql()} FROM belief WHERE valid_to IS NULL AND status='committed'"
    args: tuple[object, ...] = ()
    if subject is not None:
        sql += " AND key LIKE ?"
        args = (f"{normalize(subject)}|%",)
    sql += " ORDER BY subject, relation"
    return [{**dict(r), "volatile": volatility(r)} for r in store.conn.execute(sql, args)]


def history(store: Store, subject: str, relation: str) -> list[dict[str, object]]:
    """The full timeline for one key, oldest first, rejected candidates excluded. A retracted
    belief stays in it, with when and why it was retracted."""
    rows = store.conn.execute(
        "SELECT b.*, r.retracted_at, r.reason AS retracted_reason FROM belief b "
        "LEFT JOIN retraction r ON r.belief_id = b.id "
        "WHERE b.key=? AND b.status!='rejected' ORDER BY b.valid_from, b.id",
        (make_key(subject, relation),),
    )
    return [dict(r) for r in rows]


_SESSION_POINTER = re.compile(r"^memware:session/(.+)/turn/(\d+)$")


def pointer_session(source: object) -> str | None:
    """The session a derived belief cites, from a source written by
    :func:`memware.derive.source_pointer`; None for any other source, such as the free text a
    person passes to ``remember`` or ``assert``."""
    m = _SESSION_POINTER.match(source) if isinstance(source, str) else None
    return m.group(1) if m else None


def pointer_turn(source: object) -> int | None:
    """The turn id a derived belief cites; None for any other source, mirroring
    :func:`pointer_session`."""
    m = _SESSION_POINTER.match(source) if isinstance(source, str) else None
    return int(m.group(2)) if m else None


@dataclass(frozen=True)
class Retraction:
    """What a retraction does, or would do. Rows are belief rows as dicts.

    ``reopen`` and ``relink`` repair supersessions a retracted belief made. A reopened
    predecessor becomes current again. A relinked one stays closed, now at the next belief
    that survives, because the retracted belief had itself been superseded. ``kept`` lists
    beliefs whose source names one of the sessions but is not a session pointer: a person
    stated them, so they are never retracted."""

    sessions: list[str]
    retract: list[dict[str, Any]]
    reopen: list[dict[str, Any]]
    relink: list[dict[str, Any]]
    kept: list[dict[str, Any]]
    reasons: dict[int, str] = field(default_factory=dict)
    """Why each belief is retracted, by id, when a person chose it rather than its session
    leaving the index (:func:`plan_belief_retraction`)."""


def _cited_sessions(store: Store) -> dict[str, list[dict[str, Any]]]:
    """Committed beliefs whose source is a session pointer, by the session it names."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in store.conn.execute(
        "SELECT * FROM belief WHERE status='committed' AND source LIKE 'memware:session/%' "
        "ORDER BY id"
    ):
        session = pointer_session(r["source"])
        if session is not None:
            out.setdefault(session, []).append(dict(r))
    return out


def _indexed(store: Store, session: str) -> bool:
    row = store.conn.execute("SELECT 1 FROM turn WHERE session=? LIMIT 1", (session,)).fetchone()
    return row is not None


def orphaned_count(store: Store) -> int:
    """Committed beliefs citing a session that has no turn left in the store. This is the
    retractable kind: ``memware beliefs retract --orphaned`` acts on exactly these."""
    return sum(len(rows) for s, rows in _cited_sessions(store).items() if not _indexed(store, s))


def stale_turn_count(store: Store) -> int:
    """Committed beliefs citing a turn id that no longer exists, though the session it belongs
    to is still indexed. This is not ``orphaned_count``: the session was re-indexed and its
    turns renumbered, so the citation dangles while the evidence itself is still in the store.
    Nothing retracts these yet — there is no repair command."""
    total = 0
    for session, rows in _cited_sessions(store).items():
        if not _indexed(store, session):
            continue
        turn_ids = {
            row[0] for row in store.conn.execute("SELECT id FROM turn WHERE session=?", (session,))
        }
        total += sum(1 for r in rows if pointer_turn(r["source"]) not in turn_ids)
    return total


def _next_surviving(store: Store, start: int | None, gone: set[int]) -> sqlite3.Row | None:
    """The first belief along the ``superseded_by`` chain from ``start`` that is committed
    and not in ``gone``."""
    seen: set[int] = set()
    nxt = start
    while nxt is not None and nxt not in seen:
        seen.add(nxt)
        row = store.conn.execute(
            "SELECT id, valid_from, status, superseded_by FROM belief WHERE id=?", (nxt,)
        ).fetchone()
        if row is None:
            return None
        if row["id"] not in gone and row["status"] == "committed":
            return row  # type: ignore[no-any-return]
        nxt = row["superseded_by"]
    return None


def plan_retraction(store: Store, sessions: list[str] | None = None) -> Retraction:
    """Read-only: the committed beliefs derived from ``sessions``, and the supersession repairs
    retracting them needs. ``None`` means every session a belief cites that has no turn left in
    the store. Explicit sessions are taken as given, so a prune can plan before it deletes."""
    cited = _cited_sessions(store)
    targets = sorted(
        (s for s in cited if not _indexed(store, s)) if sessions is None else set(sessions)
    )
    retract = sorted((r for s in targets for r in cited.get(s, [])), key=lambda r: r["id"])
    reopen, relink = _repairs(store, retract)

    kept: list[dict[str, Any]] = []
    if targets:
        for b in store.conn.execute(
            "SELECT * FROM belief WHERE status='committed' AND source IS NOT NULL ORDER BY id"
        ):
            if pointer_session(b["source"]) is None and any(s in b["source"] for s in targets):
                kept.append(dict(b))
    return Retraction(targets, retract, reopen, relink, kept)


def plan_belief_retraction(store: Store, reasons: dict[int, str]) -> Retraction:
    """Read-only: the current, committed beliefs ``reasons`` names by id, each to be retracted
    for its reason. An id that names no current committed belief is left out of ``retract``: a
    superseded belief already reaches no prompt, and retracting it would move the end of its
    interval, which is history.

    Nothing is reopened or relinked. These beliefs were chosen because they no longer hold, not
    because their evidence is gone: each was true when recorded, and a value one superseded is
    older still, so it stays closed where it was."""
    ids = sorted(set(reasons))
    marks = ",".join("?" * len(ids))
    rows = [
        dict(r)
        for r in store.conn.execute(
            f"SELECT * FROM belief WHERE status='committed' AND valid_to IS NULL "
            f"AND id IN ({marks}) ORDER BY id",
            ids,
        )
    ]
    return Retraction([], rows, [], [], [], {r["id"]: reasons[r["id"]] for r in rows})


def _repairs(
    store: Store, retract: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(reopen, relink): the supersessions retracting ``retract`` leaves to repair."""
    gone = {r["id"] for r in retract}
    reopen: list[dict[str, Any]] = []
    relink: list[dict[str, Any]] = []
    for r in retract:
        for p in store.conn.execute(
            "SELECT * FROM belief WHERE superseded_by=? AND status='committed' ORDER BY id",
            (r["id"],),
        ):
            if p["id"] in gone:
                continue  # retracted too: its own predecessor is repaired past it
            repair = {**dict(p), "was_superseded_by": r["id"]}
            nxt = _next_surviving(store, r["superseded_by"], gone)
            if nxt is None:
                reopen.append({**repair, "valid_to": None, "superseded_by": None})
            else:
                relink.append({**repair, "valid_to": nxt["valid_from"], "superseded_by": nxt["id"]})
    return reopen, relink


def apply_retraction(store: Store, plan: Retraction, *, reason: str) -> None:
    """Carry out ``plan``. The caller holds the write transaction and planned inside it.

    A retracted belief is closed at its own start, as a rejected candidate is, and keeps its
    row, so ``history`` still shows it. No belief row is deleted."""
    conn, ts = store.conn, now_iso()
    notes: dict[int, list[str]] = {}
    for p in plan.reopen:
        conn.execute("UPDATE belief SET valid_to=NULL, superseded_by=NULL WHERE id=?", (p["id"],))
        notes.setdefault(p["was_superseded_by"], []).append(f"reopened #{p['id']}")
    for p in plan.relink:
        conn.execute(
            "UPDATE belief SET valid_to=?, superseded_by=? WHERE id=?",
            (p["valid_to"], p["superseded_by"], p["id"]),
        )
        notes.setdefault(p["was_superseded_by"], []).append(
            f"relinked #{p['id']} to #{p['superseded_by']}"
        )
    for r in plan.retract:
        conn.execute(
            "UPDATE belief SET status='retracted', valid_to=valid_from WHERE id=?", (r["id"],)
        )
        why = plan.reasons.get(r["id"]) or (
            f"session {pointer_session(r['source'])} is no longer indexed ({reason})"
        )
        conn.execute(
            "INSERT OR REPLACE INTO retraction(belief_id, retracted_at, reason) VALUES (?,?,?)",
            (r["id"], ts, "; ".join([why, *notes.get(r["id"], [])])),
        )


def retract(
    store: Store,
    sessions: list[str] | None = None,
    *,
    reason: str,
    apply: bool = False,
    beliefs: dict[int, str] | None = None,
) -> Retraction:
    """Retract the committed beliefs derived from ``sessions`` (``None``: every session no
    longer indexed) and repair the supersessions they made; or, given ``beliefs`` (id -> why),
    exactly those beliefs, reopening nothing (:func:`plan_belief_retraction`). Without ``apply``
    nothing is written, and the result says what would be done."""
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
    try:
        plan = (
            plan_retraction(store, sessions)
            if beliefs is None
            else plan_belief_retraction(store, beliefs)
        )
        if apply:
            apply_retraction(store, plan, reason=reason)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")
    return plan


def touch(store: Store, belief_ids: list[int]) -> None:
    """Record a retrieval (the testing effect): used beliefs rank higher later."""
    if not belief_ids:
        return
    ts = now_iso()
    store.conn.executemany(
        "UPDATE belief SET use_count=use_count+1, last_used=? WHERE id=?",
        [(ts, i) for i in belief_ids],
    )
