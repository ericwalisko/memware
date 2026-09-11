"""Stub ``memware`` MCP server for the recall-election eval.

Same three read tools as ``src/memware/mcp_server.py`` (recall, read_session, beliefs), same
server name (so the tools appear as ``mcp__memware__recall`` etc.), no store behind them.
The ``recall`` description is the variable under test: it is read from the file named by
``RECALL_DESCRIPTION_FILE`` at start-up. Every call appends one JSON line
(``{"tool", "args", "ts"}``) to ``RECALL_CALL_LOG`` so the harness can cross-check what the
model's transcript says against what the server saw.

Run: ``RECALL_DESCRIPTION_FILE=v.md RECALL_CALL_LOG=calls.jsonl python stub_server.py``
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

FALLBACK_DESCRIPTION = (
    "Search past sessions and current beliefs. Pass 3-5 phrasings of the question."
)

# Shaped like memware.index.Hit.__dict__ (id, kind, score, text, session, ts, passage_id, offset).
_TURN_HITS: list[dict[str, Any]] = [
    {
        "id": 4127,
        "kind": "turn",
        "score": 0.83,
        "text": (
            "We went with 8071 for the sidecar because 8070 collided with the metrics "
            "exporter on the shared host; documented in the runbook but not in code."
        ),
        "session": "2026-07-14-sidecar-port",
        "ts": "2026-07-14T18:22:05Z",
        "passage_id": 9931,
        "offset": 0,
    },
    {
        "id": 3980,
        "kind": "turn",
        "score": 0.61,
        "text": (
            "Tried the pooled client first; dropped it after the retry storm in staging. "
            "Single connection with backoff is what shipped."
        ),
        "session": "2026-07-02-client-retry",
        "ts": "2026-07-02T15:40:11Z",
        "passage_id": 9412,
        "offset": 112,
    },
]

_BELIEF_HITS: list[dict[str, Any]] = [
    {
        "id": 212,
        "kind": "belief",
        "score": 0.9,
        "text": "sidecar.port = 8071 (since 2026-07-14, source: session 2026-07-14-sidecar-port)",
        "session": None,
        "ts": "2026-07-14T18:25:00Z",
    },
]

_SESSION_TURNS: list[dict[str, Any]] = [
    {
        "id": 4126,
        "session": "2026-07-14-sidecar-port",
        "ts": "2026-07-14T18:21:40Z",
        "role": "user",
        "text": "why is the sidecar listening on 8071 and not 8070 like the doc says?",
    },
    {
        "id": 4127,
        "session": "2026-07-14-sidecar-port",
        "ts": "2026-07-14T18:22:05Z",
        "role": "assistant",
        "text": "8070 collides with the metrics exporter on the shared host, so we moved to 8071.",
    },
    {
        "id": 4128,
        "session": "2026-07-14-sidecar-port",
        "ts": "2026-07-14T18:23:10Z",
        "role": "user",
        "text": "ok, record that as the current value",
    },
]

_BELIEFS: list[dict[str, Any]] = [
    {
        "id": 212,
        "subject": "sidecar",
        "relation": "port",
        "value": "8071",
        "valid_from": "2026-07-14T18:25:00Z",
        "reliability": 0.8,
        "source": "session 2026-07-14-sidecar-port",
    },
    {
        "id": 198,
        "subject": "client",
        "relation": "retry_strategy",
        "value": "single connection with exponential backoff",
        "valid_from": "2026-07-02T15:45:00Z",
        "reliability": 0.7,
        "source": "session 2026-07-02-client-retry",
    },
]


def _read_description() -> str:
    path = os.environ.get("RECALL_DESCRIPTION_FILE")
    if not path:
        return FALLBACK_DESCRIPTION
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError as e:
        print(f"stub_server: cannot read RECALL_DESCRIPTION_FILE {path!r}: {e}", file=sys.stderr)
        return FALLBACK_DESCRIPTION
    return text or FALLBACK_DESCRIPTION


def _log(tool: str, args: dict[str, Any]) -> None:
    path = os.environ.get("RECALL_CALL_LOG")
    if not path:
        return
    line = json.dumps({"tool": tool, "args": args, "ts": time.time()}, ensure_ascii=False)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:  # never fail the tool call over logging
        print(f"stub_server: cannot append to RECALL_CALL_LOG {path!r}: {e}", file=sys.stderr)


def build() -> Any:
    try:  # mcp >= 2
        from mcp.server.mcpserver import MCPServer as _Server
    except ImportError:  # pragma: no cover - mcp 1.x
        try:
            from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[attr-defined,no-redef]
        except ImportError as e:
            raise SystemExit("install the 'mcp' extra: uv run --extra mcp ...") from e

    app = _Server("memware")
    description = _read_description()

    def recall(queries: list[str], k: int = 8, what: str = "all") -> list[dict[str, Any]]:
        _log("recall", {"queries": list(queries), "k": k, "what": what})
        hits: list[dict[str, Any]] = []
        if what in ("all", "beliefs"):
            hits += _BELIEF_HITS
        if what in ("all", "turns"):
            hits += _TURN_HITS
        return hits[: max(1, k)]

    # The docstring is what the client sees as the tool description; both server classes read
    # it at registration time, so set it before decorating.
    recall.__doc__ = description
    app.tool()(recall)

    @app.tool()
    def read_session(
        session: str, around: int | None = None, window: int = 5
    ) -> list[dict[str, Any]]:
        """Call this when a recall hit needs its surrounding conversation.

        Reads a session's turns whole, or a window around one turn id from recall.
        """
        _log("read_session", {"session": session, "around": around, "window": window})
        turns = [dict(t, session=session) for t in _SESSION_TURNS]
        if around is not None:
            turns = [t for t in turns if abs(int(t["id"]) - int(around)) <= max(1, window)]
        return turns or [dict(_SESSION_TURNS[1], session=session)]

    @app.tool()
    def beliefs(subject: str | None = None) -> list[dict[str, Any]]:
        """Call this for the current value of a setting or decision the ledger tracks.

        Returns currently valid beliefs, optionally filtered by subject.
        """
        _log("beliefs", {"subject": subject})
        if subject:
            hit = [b for b in _BELIEFS if subject.lower() in str(b["subject"]).lower()]
            return hit or _BELIEFS[:1]
        return _BELIEFS

    return app


def main() -> None:
    build().run()


if __name__ == "__main__":
    main()
