"""Recall over turns and beliefs: BM25 x activation, no model in the loop.

Ranking follows the rational analysis of memory (Anderson & Schooler, 1991):
the probability a memory is needed is a power function of how recently and
how often it was needed. ACT-R writes base-level activation as
``ln(sum(t_j ** -d))``; we use the same shape on top of BM25 relevance::

    score = bm25 * (1 + age_days) ** -decay * (1 + use_weight * ln(1 + use_count))

Retrieval is recorded (``use_count``/``last_used``) so what gets used gets
easier to find — the testing effect.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from memware.store import Store, now_iso

_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-./]{1,}")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "about",
        "there",
        "their",
        "then",
        "than",
        "not",
        "no",
        "yes",
        "me",
        "my",
        "our",
        "us",
    ]
)


@dataclass(frozen=True)
class Hit:
    id: int
    kind: str
    score: float
    text: str
    session: str | None = None
    ts: str | None = None
    role: str | None = None
    subject: str | None = None
    relation: str | None = None
    source: str | None = None
    snippet: str | None = None


def fts_query(text: str, max_terms: int = 24) -> str:
    """Turn free text into an OR-joined FTS5 query of quoted keywords."""
    terms: list[str] = []
    for tok in _TOKEN.findall(text):
        t = tok.lower().strip(".-/")
        if len(t) < 2 or t in STOPWORDS or t in terms:
            continue
        terms.append(t)
        if len(terms) >= max_terms:
            break
    return " OR ".join(f'"{t}"' for t in terms)


def _age_days(ts: str | None) -> float:
    if not ts:
        return 365.0
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 365.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - t).total_seconds() / 86400.0)


def activation(ts: str | None, use_count: int, *, decay: float, use_weight: float) -> float:
    return float((1.0 + _age_days(ts)) ** (-decay)) * (
        1.0 + use_weight * math.log1p(max(0, use_count))
    )


def search_turns(
    store: Store,
    query: str,
    *,
    k: int = 10,
    decay: float = 0.5,
    use_weight: float = 0.5,
    candidates: int = 100,
    record_use: bool = True,
    snippet_tokens: int = 48,
) -> list[Hit]:
    """Top-k turns by BM25 x activation. Empty query -> no hits.

    Each hit carries ``snippet``: the FTS5 window around the matched terms, which is what
    a prompt should quote — the head of a long turn often does not contain the answer.
    """
    q = fts_query(query)
    if not q:
        return []
    rows = store.conn.execute(
        "SELECT t.id, t.session, t.ts, t.role, t.text, t.source, t.use_count, "
        "-bm25(turn_fts) AS rel, snippet(turn_fts, 0, '', '', ' … ', ?) AS snip "
        "FROM turn_fts JOIN turn t ON t.id = turn_fts.rowid "
        "WHERE turn_fts MATCH ? ORDER BY rel DESC LIMIT ?",
        (snippet_tokens, q, candidates),
    ).fetchall()
    hits = [
        Hit(
            id=r["id"],
            kind="turn",
            score=r["rel"]
            * activation(r["ts"], r["use_count"], decay=decay, use_weight=use_weight),
            text=r["text"],
            session=r["session"],
            ts=r["ts"],
            role=r["role"],
            source=r["source"],
            snippet=r["snip"],
        )
        for r in rows
    ]
    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:k]
    if record_use and hits:
        ts = now_iso()
        store.conn.executemany(
            "UPDATE turn SET use_count=use_count+1, last_used=? WHERE id=?",
            [(ts, h.id) for h in hits],
        )
    return hits


def search_beliefs(
    store: Store,
    query: str,
    *,
    k: int = 10,
    decay: float = 0.1,
    use_weight: float = 0.5,
    record_use: bool = True,
) -> list[Hit]:
    """Top-k *currently valid, committed* beliefs. Superseded values never surface."""
    q = fts_query(query)
    if not q:
        return []
    rows = store.conn.execute(
        "SELECT b.*, -bm25(belief_fts) AS rel FROM belief_fts "
        "JOIN belief b ON b.id = belief_fts.rowid "
        "WHERE belief_fts MATCH ? AND b.valid_to IS NULL AND b.status='committed' "
        "ORDER BY rel DESC LIMIT 100",
        (q,),
    ).fetchall()
    hits = [
        Hit(
            id=r["id"],
            kind="belief",
            score=r["rel"]
            * float(r["reliability"])
            * activation(
                r["last_used"] or r["recorded_at"],
                r["use_count"],
                decay=decay,
                use_weight=use_weight,
            ),
            text=f"{r['subject']} {r['relation']} {r['value']}",
            subject=r["subject"],
            relation=r["relation"],
            ts=r["valid_from"],
            source=r["source"],
        )
        for r in rows
    ]
    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:k]
    if record_use and hits:
        from memware.ledger import touch

        touch(store, [h.id for h in hits])
    return hits


def read_turns(
    store: Store, session: str, *, around: int | None = None, window: int = 5
) -> list[dict[str, object]]:
    """Read a session's turns in order, optionally a window around one turn id."""
    if around is None:
        rows = store.conn.execute(
            "SELECT * FROM turn WHERE session=? ORDER BY seq", (session,)
        ).fetchall()
    else:
        anchor = store.conn.execute("SELECT seq FROM turn WHERE id=?", (around,)).fetchone()
        if anchor is None:
            return []
        rows = store.conn.execute(
            "SELECT * FROM turn WHERE session=? AND seq BETWEEN ? AND ? ORDER BY seq",
            (session, anchor["seq"] - window, anchor["seq"] + window),
        ).fetchall()
    return [dict(r) for r in rows]
