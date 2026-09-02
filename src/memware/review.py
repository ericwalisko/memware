"""A generic review channel for contested supersessions.

When a candidate challenges a committed belief, the ledger parks it and opens a
review. How a human sees and answers that review is your business — a chat
bot, a web page, a phone app, a spreadsheet. memware only defines the contract:

* :class:`ReviewBackend` — ``publish`` open reviews somewhere, ``collect``
  decisions back.
* :class:`JsonlReviewBackend` — files: an outbox you read, an inbox you append
  decisions to. Zero infrastructure.
* :class:`HttpReviewBackend` — POST open reviews to a URL, GET decisions.

``sync_reviews`` drives either: apply what was decided, publish what is open.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from memware.ledger import approve, reject
from memware.store import Store, now_iso


@dataclass(frozen=True)
class ReviewItem:
    review_id: int
    subject: str
    relation: str
    candidate_value: str
    candidate_source: str | None
    candidate_valid_from: str
    candidate_reliability: float
    incumbent_value: str | None
    incumbent_source: str | None
    incumbent_valid_from: str | None
    incumbent_reliability: float | None
    reason: str
    opened_at: str


@dataclass(frozen=True)
class Decision:
    review_id: int
    decision: str
    note: str | None = None


class ReviewBackend(Protocol):
    def publish(self, items: list[ReviewItem]) -> None: ...

    def collect(self) -> list[Decision]: ...


def open_reviews(store: Store) -> list[ReviewItem]:
    rows = store.conn.execute(
        "SELECT r.id AS review_id, r.reason, r.opened_at, "
        "c.subject, c.relation, c.value AS cv, c.source AS cs, c.valid_from AS cvf, "
        "c.reliability AS cr, i.value AS iv, i.source AS isrc, i.valid_from AS ivf, "
        "i.reliability AS ir "
        "FROM review r JOIN belief c ON c.id=r.belief_id "
        "LEFT JOIN belief i ON i.id=r.incumbent_id "
        "WHERE r.decision IS NULL ORDER BY r.id"
    ).fetchall()
    return [
        ReviewItem(
            review_id=r["review_id"],
            subject=r["subject"],
            relation=r["relation"],
            candidate_value=r["cv"],
            candidate_source=r["cs"],
            candidate_valid_from=r["cvf"],
            candidate_reliability=float(r["cr"]),
            incumbent_value=r["iv"],
            incumbent_source=r["isrc"],
            incumbent_valid_from=r["ivf"],
            incumbent_reliability=float(r["ir"]) if r["ir"] is not None else None,
            reason=r["reason"],
            opened_at=r["opened_at"],
        )
        for r in rows
    ]


def apply_decision(store: Store, d: Decision) -> str:
    if d.decision == "approve":
        approve(store, d.review_id)
    elif d.decision == "reject":
        reject(store, d.review_id)
    else:
        raise ValueError(f"unknown decision {d.decision!r}")
    return d.decision


def sync_reviews(store: Store, backend: ReviewBackend) -> dict[str, int]:
    """Apply any decisions the backend has collected, then publish what is still open."""
    applied = 0
    for d in backend.collect():
        try:
            apply_decision(store, d)
            applied += 1
        except LookupError:
            continue
    items = open_reviews(store)
    if items:
        backend.publish(items)
    return {"published": len(items), "applied": applied}


class JsonlReviewBackend:
    """Outbox: one JSON object per open review (rewritten on each publish).
    Inbox: append ``{"review_id": 12, "decision": "approve"}`` lines; consumed on collect."""

    def __init__(self, outbox: str | Path, inbox: str | Path) -> None:
        self.outbox = Path(outbox).expanduser()
        self.inbox = Path(inbox).expanduser()

    def publish(self, items: list[ReviewItem]) -> None:
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox.open("w", encoding="utf-8") as fh:
            for it in items:
                fh.write(json.dumps({**asdict(it), "published_at": now_iso()}) + "\n")

    def collect(self) -> list[Decision]:
        if not self.inbox.exists():
            return []
        out: list[Decision] = []
        for raw in self.inbox.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append(Decision(int(d["review_id"]), str(d["decision"]), d.get("note")))
        self.inbox.write_text("", encoding="utf-8")
        return out


class HttpReviewBackend:
    """``POST {base}/reviews`` with ``{"items": [...]}``; ``GET {base}/decisions`` returns
    ``{"decisions": [{"review_id", "decision", "note"}]}``. Bearer auth optional."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 10.0) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        result: dict[str, object] = json.loads(raw) if raw else {}
        return result

    def publish(self, items: list[ReviewItem]) -> None:
        self._req("POST", "/reviews", {"items": [asdict(i) for i in items]})

    def collect(self) -> list[Decision]:
        payload = self._req("GET", "/decisions")
        decisions = payload.get("decisions", [])
        if not isinstance(decisions, list):
            return []
        return [
            Decision(int(d["review_id"]), str(d["decision"]), d.get("note"))
            for d in decisions
            if isinstance(d, dict)
        ]
