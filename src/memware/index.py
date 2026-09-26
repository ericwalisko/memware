"""Recall over turns and beliefs: BM25 x activation, no model in the loop.

Ranking follows the rational analysis of memory (Anderson & Schooler, 1991):
the probability a memory is needed is a power function of how recently and
how often it was needed. ACT-R writes base-level activation as
``ln(sum(t_j ** -d))``; we use the same shape on top of BM25 relevance::

    score = bm25 * (1 + age_days) ** -decay * (1 + use_weight * ln(1 + use_count))

Retrieval is recorded (``use_count``/``last_used``) so what gets used gets
easier to find — the testing effect.

BM25 runs over *passages* (see :mod:`memware.passage`), not whole turns, while
activation stays the turn's — recency and use belong to the conversation. Hits
collapse to one per turn and quote only the matching passages, so recall costs
roughly a third fewer tokens; reading a session back still returns whole turns.

Results also collapse across turns whose quoted text is byte-identical — the same
scheduled-automation prompt captured on many days would otherwise take several slots
with copies of one string. The highest-ranked copy is kept; the turns themselves stay
in the store, so a session still reads back whole and distinct findings still surface.
"""

from __future__ import annotations

import math
import re
import sqlite3
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime

from memware.store import Store, now_iso
from memware.term import ellipsis

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
    """One ranked result. ``id`` is the record you can read back — the turn id for
    a turn hit (:func:`read_turns` takes it as ``around``), the belief id otherwise.

    For turn hits ``text`` is the matching passage or passages rather than the whole
    turn; ``passage_id`` is the best-scoring one and ``offset`` the character where
    the quoted text starts inside the turn.

    For belief hits ``volatile`` is the class (:mod:`memware.volatile`) of a derived belief
    that was a measurement, a moving version or a status when recorded, and None otherwise.
    """

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
    passage_id: int | None = None
    offset: int | None = None
    volatile: str | None = None


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


ELLIPSIS = " … "


def _join_passages(rows: list[sqlite3.Row]) -> str:
    """Passage texts in document order; an ellipsis stands in for what was skipped."""
    parts: list[str] = []
    previous: int | None = None
    for r in rows:
        if previous is not None and r["ord"] != previous + 1:
            parts.append(f" {ellipsis()} ")
        parts.append(r["passage_text"])
        previous = int(r["ord"])
    return "".join(parts)


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
    candidates: int = 200,
    record_use: bool = True,
    snippet_tokens: int = 96,
    passages_per_turn: int = 3,
    collapse_duplicates: bool = True,
) -> list[Hit]:
    """Top-k turns by BM25 x activation, each ranked on its best passage.

    Ranking is per passage, but hits collapse to one per turn, so ``k`` still
    means k distinct turns. ``text`` is that turn's ``passages_per_turn`` best
    matching passages in document order, joined by an ellipsis where they are not
    adjacent — the quotable context, a third to a half the size of the whole turn.
    ``snippet`` is the FTS5 window inside the best passage; ``id`` is the turn,
    which :func:`read_turns` reads back whole.

    Quoting fewer passages is cheaper but loses facts, because the answer and the
    question's vocabulary often sit in different parts of one turn: on a 24-question
    fact set, 1 passage scored 21/24, 2 scored 23/24 and 3 scored 24/24 — matching
    whole-turn recall on half the context. See docs/eval.md.

    Empty query -> no hits.
    """
    q = fts_query(query)
    if not q:
        return []
    mark = ellipsis()  # our own constant (…/...); not user input
    rows = store.conn.execute(
        "SELECT p.id AS passage_id, p.turn_id, p.ord, p.start_char, p.text AS passage_text, "
        "t.session, t.ts, t.role, t.source, t.use_count, "
        f"-bm25(passage_fts) AS rel, snippet(passage_fts, 0, '', '', ' {mark} ', ?) AS snip "
        "FROM passage_fts JOIN passage p ON p.id = passage_fts.rowid "
        "JOIN turn t ON t.id = p.turn_id "
        "WHERE passage_fts MATCH ? ORDER BY rel DESC LIMIT ?",
        (snippet_tokens, q, candidates),
    ).fetchall()
    scored: dict[int, list[tuple[float, sqlite3.Row]]] = {}
    for r in rows:
        score = r["rel"] * activation(r["ts"], r["use_count"], decay=decay, use_weight=use_weight)
        scored.setdefault(r["turn_id"], []).append((score, r))
    hits: list[Hit] = []
    seen_text: set[str] = set()
    for turn_id in sorted(scored, key=lambda t: -max(s for s, _ in scored[t])):
        if len(hits) >= k:
            break
        by_score = sorted(scored[turn_id], key=lambda sr: -sr[0])
        keep = sorted(by_score[: max(1, passages_per_turn)], key=lambda sr: sr[1]["ord"])
        text = _join_passages([r for _, r in keep])
        if collapse_duplicates:
            sig = text.strip()
            if sig in seen_text:
                continue  # byte-identical to a higher-ranked hit (e.g. a repeated cron prompt)
            seen_text.add(sig)
        best = by_score[0][1]
        hits.append(
            Hit(
                id=turn_id,
                kind="turn",
                score=by_score[0][0],
                text=text,
                session=best["session"],
                ts=best["ts"],
                role=best["role"],
                source=best["source"],
                snippet=best["snip"],
                passage_id=best["passage_id"],
                offset=keep[0][1]["start_char"],
            )
        )
    if record_use and hits:
        ts = now_iso()
        store.try_write(  # a use count waits a moment for the lock, never a scrub
            "UPDATE turn SET use_count=use_count+1, last_used=? WHERE id=?",
            [(ts, h.id) for h in hits],
        )
    return hits


def _subject_terms(subject: str) -> set[str]:
    return {t.lower().strip(".-/") for t in _TOKEN.findall(subject)} - STOPWORDS


def _query_terms(q: str) -> set[str]:
    """The terms of an :func:`fts_query` string."""
    return {t.strip('"') for t in q.split(" OR ")} if q else set()


RARE_SHARE = 0.10
"""A word in at most this share of indexed passages is rare enough for one shared subject word to
carry an unsolicited injection. On one user's labeled prompts, the rule it sets (two shared words
always pass) kept every relevant belief and cut 30.3% of the noise; docs/relevance-calibration.md
has the method and the numbers."""

RARITY_MIN_PASSAGES = 1_000
"""Below this many indexed passages the share is not read, and one shared subject word is enough,
as before. A share needs a sample: at 1,000 passages the 10% line sits 100 passages up and a word's
share is known to about ±2 points (binomial, 95%), where at 100 passages it is ±6 and a word at 5%
cannot be told from one at 15%. Passages come in conversations, which makes the real error larger,
and a young store is a few conversations whose own subject would read as a common word. Past the
floor the rule decides; under it nothing changes."""


class _PassageVocab:
    """How many indexed passages hold a term, read through an ``fts5vocab`` table in the temp
    schema. Creating that writes nothing to the store, so a read-only connection can use it. Each
    term is read once; the store's ``porter unicode61`` index keeps stems, so a term is looked up
    exactly as the subject writes it, and one it does not hold counts as absent."""

    _TABLE = "temp.memware_passage_vocab"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.stamp = self._stamp(conn)
        self._passages: int | None = None
        self._docs: dict[str, int] = {}

    @property
    def passages(self) -> int:
        if self._passages is None:
            self._passages = int(self.conn.execute("SELECT count(*) FROM passage").fetchone()[0])
        return self._passages

    @staticmethod
    def _stamp(conn: sqlite3.Connection) -> tuple[int, int]:
        """Changes this connection has made, and whether another has committed since
        (``data_version``): what the counts are good for."""
        return conn.total_changes, int(conn.execute("PRAGMA data_version").fetchone()[0])

    def current(self) -> bool:
        return self._stamp(self.conn) == self.stamp

    def share(self, term: str) -> float:
        """The share of indexed passages that hold ``term``; 0.0 when the index has no such term."""
        if term not in self._docs:
            if not self._docs:
                self.conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._TABLE} "
                    "USING fts5vocab(main, passage_fts, row)"
                )
            row = self.conn.execute(
                f"SELECT doc FROM {self._TABLE} WHERE term = ?", (term,)
            ).fetchone()
            self._docs[term] = int(row[0]) if row else 0
        return self._docs[term] / self.passages if self.passages else 0.0


_VOCABS: weakref.WeakKeyDictionary[object, _PassageVocab] = weakref.WeakKeyDictionary()


def _vocab(store: Store | sqlite3.Connection) -> _PassageVocab:
    """The passage vocabulary for ``store``, kept for as long as the store object lives and
    nothing changes the database. A bare connection cannot be weakly referenced, so it is read
    fresh on each call."""
    if isinstance(store, sqlite3.Connection):
        return _PassageVocab(store)
    vocab = _VOCABS.get(store)
    if vocab is None or vocab.conn is not store.conn or not vocab.current():
        vocab = _VOCABS[store] = _PassageVocab(store.conn)
    return vocab


def _names_subject(vocab: _PassageVocab, matched: set[str]) -> bool:
    """The subject rule over the subject terms a query shares: two or more pass; one passes if it
    is rare in the indexed passages (:data:`RARE_SHARE`), or if the store is too small to say
    (:data:`RARITY_MIN_PASSAGES`); none fails."""
    if len(matched) != 1:
        return len(matched) > 1
    if vocab.passages < RARITY_MIN_PASSAGES:
        return True
    return vocab.share(next(iter(matched))) <= RARE_SHARE


def subject_passes(store: Store | sqlite3.Connection, query: str, subject: str) -> bool:
    """Whether a belief with ``subject`` passes the subject rule for ``query``: the test
    ``search_beliefs(..., require_subject=True)`` applies before ranking, and so the prompt hook,
    Hermes prefetch and ``memware eval``. It reads only the subject. It writes nothing, so it works
    on a read-only connection (``sqlite3.connect("file:...?mode=ro", uri=True)``).

    It passes when the subject and the query share two or more distinct terms, or one term that at
    most :data:`RARE_SHARE` of the indexed passages hold. A term missing from the passage index as
    written counts as rare: ids, versions and paths tokenize into pieces, and they are specific.
    Below :data:`RARITY_MIN_PASSAGES` passages one shared term is enough. Pass the
    :class:`~memware.store.Store` (or any object whose ``conn`` is the connection) to reuse the
    passage counts across calls."""
    matched = _subject_terms(subject) & _query_terms(fts_query(query))
    return bool(matched) and _names_subject(_vocab(store), matched)


def search_beliefs(
    store: Store,
    query: str,
    *,
    k: int = 10,
    decay: float = 0.1,
    use_weight: float = 0.5,
    record_use: bool = True,
    require_subject: bool = False,
) -> list[Hit]:
    """Top-k *current, committed* beliefs. Superseded values never surface. A derived belief that
    was volatile when recorded still surfaces, marked with its class in ``volatile``.

    ``require_subject=True`` keeps only beliefs whose *subject* the query names
    (:func:`subject_passes`). Use it for unsolicited prompt-time injection: relation and value
    words ("decision", "recovery", "model") match almost any prompt, and a belief about
    the wrong subject is noise, not memory. So is one whose subject shares only a word the
    user's conversations use everywhere: "feedback to file" is not about "config file location".
    """
    from memware.ledger import confirmed_sql
    from memware.volatile import volatility

    q = fts_query(query)
    if not q:
        return []
    rows = store.conn.execute(
        f"SELECT b.*, {confirmed_sql('b')}, -bm25(belief_fts, 3.0, 1.0, 1.0) AS rel FROM belief_fts "
        "JOIN belief b ON b.id = belief_fts.rowid "
        "WHERE belief_fts MATCH ? AND b.valid_to IS NULL AND b.status='committed' "
        "ORDER BY rel DESC LIMIT 100",
        (q,),
    ).fetchall()
    if require_subject:
        qterms, vocab = _query_terms(q), _vocab(store)
        rows = [r for r in rows if _names_subject(vocab, _subject_terms(r["subject"]) & qterms)]
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
            volatile=volatility(r),
        )
        for r in rows
    ]
    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:k]
    if record_use and hits:
        from memware.ledger import touch

        touch(store, [h.id for h in hits])
    return hits


def _rrf(ranked_lists: list[list[Hit]], k: int, c: int = 60) -> list[Hit]:
    """Reciprocal-rank fusion across several ranked lists; keeps the best Hit object per id."""
    score: dict[tuple[str, int], float] = {}
    best: dict[tuple[str, int], Hit] = {}
    for hits in ranked_lists:
        for rank, h in enumerate(hits):
            key = (h.kind, h.id)
            score[key] = score.get(key, 0.0) + 1.0 / (c + rank + 1)
            if key not in best or h.score > best[key].score:
                best[key] = h
    order = sorted(score, key=lambda kk: score[kk], reverse=True)[:k]
    return [best[kk] for kk in order]


def _collapse_identical(hits: list[Hit], k: int) -> list[Hit]:
    """Drop turn hits whose quoted text is byte-identical to a higher-ranked one, then take the
    first ``k``. Non-turn hits pass through. This is the cross-phrasing counterpart to the
    per-query collapse in :func:`search_turns`: reciprocal-rank fusion keys on ``(kind, id)``,
    so two different turns holding the same repeated text would otherwise both survive."""
    seen: set[str] = set()
    out: list[Hit] = []
    for h in hits:
        if h.kind == "turn":
            sig = h.text.strip()
            if sig in seen:
                continue
            seen.add(sig)
        out.append(h)
        if len(out) >= k:
            break
    return out


def search_turns_multi(
    store: Store,
    queries: list[str],
    *,
    k: int = 10,
    record_use: bool = True,
    **kw: object,
) -> list[Hit]:
    """Recall over several phrasings at once (synonyms, related concepts, literal values
    the caller expects to see) fused by reciprocal rank. This is how a tool-calling agent
    puts its own reasoning into retrieval: it supplies the variants, the index stays
    model-free. Empty or duplicate phrasings are ignored."""
    seen: list[str] = []
    for q in queries:
        q = (q or "").strip()
        if q and q.lower() not in {x.lower() for x in seen}:
            seen.append(q)
    if not seen:
        return []
    if len(seen) == 1:
        return search_turns(store, seen[0], k=k, record_use=record_use, **kw)  # type: ignore[arg-type]
    collapse = bool(kw.get("collapse_duplicates", True))
    lists = [search_turns(store, q, k=max(k, 20), record_use=False, **kw) for q in seen]  # type: ignore[arg-type]
    fused = _rrf(lists, max(k * 3, 30) if collapse else k)
    fused = _collapse_identical(fused, k) if collapse else fused[:k]
    if record_use and fused:
        ts = now_iso()
        store.try_write(
            "UPDATE turn SET use_count=use_count+1, last_used=? WHERE id=?",
            [(ts, h.id) for h in fused],
        )
    return fused


def search_beliefs_multi(
    store: Store, queries: list[str], *, k: int = 10, record_use: bool = True, **kw: object
) -> list[Hit]:
    """Belief recall over several phrasings, fused by reciprocal rank."""
    seen = [q.strip() for q in queries if q and q.strip()]
    if not seen:
        return []
    lists = [search_beliefs(store, q, k=max(k, 20), record_use=False, **kw) for q in seen]  # type: ignore[arg-type]
    fused = _rrf(lists, k)
    if record_use and fused:
        from memware.ledger import touch

        touch(store, [h.id for h in fused])
    return fused


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
