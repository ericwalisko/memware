"""Optional MCP server (``pip install "memware[mcp]"``) exposing recall to any MCP client.

A tool's arguments come from a model, which may be steered by whatever it last read, so each is
bounded here before it reaches the store (docs/security.md). No tool takes a path: a session id is
only an SQL parameter, and the store is ``MEMWARE_DB``, fixed when the server starts."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from memware.index import read_turns, search_beliefs_multi, search_turns_multi
from memware.ledger import Policy, assert_belief, current
from memware.review import open_reviews
from memware.store import SHORT_WAIT_MS, Store

MAX_QUERIES = 16
"""Phrasings one recall call reads; the tool asks for 3-5. Each is a search of its own."""
MAX_K = 50
"""Hits one recall call returns per kind."""
MAX_WINDOW = 50
"""Turns read_session returns on each side of ``around``."""
FUTURE_SLACK = timedelta(days=1)
"""How far ahead of the clock a ``valid_from`` may be: time zones, not the future."""
_SQLITE_INT = 2**63


def _bounded(n: int, lo: int, hi: int) -> int:
    return max(lo, min(int(n), hi))


def event_time(raw: str | None, now: datetime | None = None) -> str | None:
    """``valid_from`` as the ledger writes it (UTC, to the second), or None for now. Refuses text
    that is not an ISO-8601 time, and a time more than :data:`FUTURE_SLACK` ahead: the ledger orders
    a key's values by it, so it must be a real time."""
    if raw is None or not str(raw).strip():
        return None
    try:
        t = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"valid_from must be an ISO-8601 time, not {raw!r}") from e
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    if t > (now or datetime.now(UTC)) + FUTURE_SLACK:
        raise ValueError(
            f"valid_from {raw!r} is in the future; a belief holds from when it was said"
        )
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def recall_hits(
    db: str | None, queries: list[str], k: int = 8, what: str = "all"
) -> list[dict[str, Any]]:
    """The ``recall`` tool: at most :data:`MAX_QUERIES` phrasings, ``k`` within 1..:data:`MAX_K`."""
    queries, k = list(queries)[:MAX_QUERIES], _bounded(k, 1, MAX_K)
    with Store(db, busy_timeout_ms=SHORT_WAIT_MS) as s:  # reads, and use counts it may skip
        hits = []
        if what in ("all", "beliefs"):
            hits += search_beliefs_multi(s, queries, k=k)
        if what in ("all", "turns"):
            hits += search_turns_multi(s, queries, k=k)
        return [h.__dict__ for h in hits]


def session_turns(
    db: str | None, session: str, around: int | None = None, window: int = 5
) -> list[dict[str, Any]]:
    """The ``read_session`` tool: ``window`` within 0..:data:`MAX_WINDOW`; an ``around`` no turn id
    can be reads nothing rather than overflow SQLite's integer."""
    if around is not None and not -_SQLITE_INT <= int(around) < _SQLITE_INT:
        return []
    with Store(db, busy_timeout_ms=SHORT_WAIT_MS) as s:
        return read_turns(s, session, around=around, window=_bounded(window, 0, MAX_WINDOW))


def remember_belief(
    db: str | None,
    subject: str,
    relation: str,
    value: str,
    source: str | None = None,
    reliability: float = 0.5,
    valid_from: str | None = None,
) -> dict[str, Any]:
    """The ``remember`` tool, with ``valid_from`` checked by :func:`event_time`."""
    vf = event_time(valid_from)
    with Store(db) as s:
        r = assert_belief(
            s,
            subject,
            relation,
            value,
            source=source,
            reliability=reliability,
            valid_from=vf,
            policy=Policy.GATE_CONFLICTS,
        )
        return {"outcome": r.outcome.value, "belief_id": r.belief_id, "review_id": r.review_id}


def build() -> Any:
    try:  # mcp >= 2
        from mcp.server.mcpserver import MCPServer as _Server
    except ImportError:  # pragma: no cover - mcp 1.x
        try:
            from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[attr-defined,no-redef]
        except ImportError as e:
            raise SystemExit("install the 'mcp' extra: pip install 'memware[mcp]'") from e

    db = os.environ.get("MEMWARE_DB")
    app = _Server("memware")

    @app.tool()
    def recall(queries: list[str], k: int = 8, what: str = "all") -> list[dict[str, Any]]:
        """Call this when the question involves:
        - a past decision or its rationale ("why did we choose X?")
        - a rejected alternative
        - why something is the way it is
        - work from an earlier session
        - anything cross-repo
        - anything not in the working tree: a value said in chat, a quoted number,
          what was tried before
        And before answering "I don't know" or re-deriving something likely settled.
        Not for: finding code in the tree now (grep it).

        Searches past sessions and current beliefs. Pass 3-5 phrasings: the question, synonyms,
        related concepts, the literal value you expect (a port, file name, version). The index
        is keyword-based; fusing your phrasings makes it semantic. what: all|turns|beliefs.
        Pass a turn hit's ``id`` to read_session's ``around`` for the full turn.
        """
        return recall_hits(db, queries, k=k, what=what)

    @app.tool()
    def read_session(
        session: str, around: int | None = None, window: int = 5
    ) -> list[dict[str, Any]]:
        """Call this when a recall hit needs its surrounding conversation.

        Reads a session's turns whole, or a window around one turn id from recall.
        """
        return session_turns(db, session, around=around, window=window)

    @app.tool()
    def beliefs(subject: str | None = None) -> list[dict[str, Any]]:
        """Call this for the current value of a setting or decision the ledger tracks.

        Returns current beliefs, optionally filtered by subject, each with ``valid_from`` (when it
        was recorded). ``volatile`` names a derived measurement, moving version or status: true
        when recorded, and worth re-checking before relying on it.
        """
        with Store(db, busy_timeout_ms=SHORT_WAIT_MS) as s:
            return current(s, subject)

    @app.tool()
    def remember(
        subject: str,
        relation: str,
        value: str,
        source: str | None = None,
        reliability: float = 0.5,
        valid_from: str | None = None,
    ) -> dict[str, Any]:
        """Record a belief. A new value for an existing (subject, relation) supersedes the old."""
        return remember_belief(db, subject, relation, value, source, reliability, valid_from)

    @app.tool()
    def pending_reviews() -> list[dict[str, Any]]:
        """Contested supersessions awaiting a human decision."""
        with Store(db, busy_timeout_ms=SHORT_WAIT_MS) as s:
            return [r.__dict__ for r in open_reviews(s)]

    return app


def main() -> None:
    build().run()


if __name__ == "__main__":
    main()
