"""Optional MCP server (``pip install "memware[mcp]"``) exposing recall to any MCP client."""

from __future__ import annotations

import os
from typing import Any

from memware.index import read_turns, search_beliefs_multi, search_turns_multi
from memware.ledger import Policy, assert_belief, current
from memware.review import open_reviews
from memware.store import Store


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
        with Store(db) as s:
            hits = []
            if what in ("all", "beliefs"):
                hits += search_beliefs_multi(s, queries, k=k)
            if what in ("all", "turns"):
                hits += search_turns_multi(s, queries, k=k)
            return [h.__dict__ for h in hits]

    @app.tool()
    def read_session(
        session: str, around: int | None = None, window: int = 5
    ) -> list[dict[str, Any]]:
        """Call this when a recall hit needs its surrounding conversation.

        Reads a session's turns whole, or a window around one turn id from recall.
        """
        with Store(db) as s:
            return read_turns(s, session, around=around, window=window)

    @app.tool()
    def beliefs(subject: str | None = None) -> list[dict[str, Any]]:
        """Call this for the current value of a setting or decision the ledger tracks.

        Returns currently valid beliefs, optionally filtered by subject.
        """
        with Store(db) as s:
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
        with Store(db) as s:
            r = assert_belief(
                s,
                subject,
                relation,
                value,
                source=source,
                reliability=reliability,
                valid_from=valid_from,
                policy=Policy.GATE_CONFLICTS,
            )
            return {"outcome": r.outcome.value, "belief_id": r.belief_id, "review_id": r.review_id}

    @app.tool()
    def pending_reviews() -> list[dict[str, Any]]:
        """Contested supersessions awaiting a human decision."""
        with Store(db) as s:
            return [r.__dict__ for r in open_reviews(s)]

    return app


def main() -> None:
    build().run()


if __name__ == "__main__":
    main()
