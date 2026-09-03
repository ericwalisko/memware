"""memware memory provider for Hermes Agent.

Implements Hermes's ``MemoryProvider`` ABC (developer guide: "Building a Memory
Provider Plugin"). Install by copying this directory to
``$HERMES_HOME/plugins/memware/`` and selecting it with ``hermes memory setup``.

Design:
* One store shared with every other memware client (Claude Code, the CLI, MCP)
  — ``db_path`` defaults to ``~/.memware/memware.db``. Set it per profile if you
  want isolation instead of sharing.
* ``prefetch`` injects only currently valid beliefs (small, bounded, never stale).
  Transcript recall is on demand through the ``memware_recall`` tool.
* ``sync_turn`` is non-blocking: each completed turn is appended to a per-session
  JSONL file under ``<hermes_home>/memware/sessions/`` (which doubles as an
  archive) and indexed from a byte-offset cursor in a daemon thread.
* Built-in memory writes are mirrored into the ledger as human-stated beliefs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

try:  # inside Hermes
    from agent.memory_provider import MemoryProvider
except ImportError:  # tests / standalone import

    class MemoryProvider:  # type: ignore[no-redef]
        """Stand-in for Hermes's ABC when Hermes is not importable."""


logger = logging.getLogger("memware.hermes")

_DEFAULT_DB = "~/.memware/memware.db"
_TOOLS: list[dict[str, Any]] = [
    {
        "name": "memware_recall",
        "description": "Search past sessions and currently valid beliefs. Returns ranked, dated "
        "snippets. Call it before answering anything about prior work; call again with "
        "different words if the first hits are not it.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 8},
                "what": {"type": "string", "enum": ["all", "turns", "beliefs"], "default": "all"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memware_read_session",
        "description": "Read a past session's turns in order, or a window around one turn id "
        "returned by memware_recall.",
        "parameters": {
            "type": "object",
            "properties": {
                "session": {"type": "string"},
                "around": {"type": "integer"},
                "window": {"type": "integer", "default": 5},
            },
            "required": ["session"],
        },
    },
    {
        "name": "memware_remember",
        "description": "Record a fact as (subject, relation, value). A new value for the same "
        "subject and relation supersedes the old one; use it the moment you learn a "
        "previously known fact has changed.",
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "relation": {"type": "string"},
                "value": {"type": "string"},
                "source": {"type": "string"},
                "reliability": {"type": "number", "default": 0.7},
            },
            "required": ["subject", "relation", "value"],
        },
    },
    {
        "name": "memware_beliefs",
        "description": "List currently valid beliefs, optionally for one subject.",
        "parameters": {"type": "object", "properties": {"subject": {"type": "string"}}},
    },
]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class MemwareProvider(MemoryProvider):
    def __init__(self) -> None:
        self._db = os.path.expanduser(_DEFAULT_DB)
        self._home = Path(os.path.expanduser("~/.hermes"))
        self._session_id = ""
        self._prefetch_k = 6
        self._auto_sync = True
        self._lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None

    # -- identity / lifecycle -------------------------------------------------
    @property
    def name(self) -> str:
        return "memware"

    def is_available(self) -> bool:
        try:
            import memware  # noqa: F401
        except ImportError:
            return False
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id or "hermes"
        home = kwargs.get("hermes_home")
        if home:
            self._home = Path(home)
        cfg = self._home / "memware.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text())
                self._db = os.path.expanduser(str(data.get("db_path") or _DEFAULT_DB))
                self._prefetch_k = int(data.get("prefetch_k", 6))
                self._auto_sync = bool(data.get("auto_sync", True))
            except (OSError, ValueError) as e:
                logger.warning("memware: bad config %s: %s", cfg, e)
        Path(self._db).parent.mkdir(parents=True, exist_ok=True)
        from memware.store import Store

        Store(self._db).close()  # create schema eagerly so first prefetch is cheap

    def shutdown(self, **kwargs: Any) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

    # -- config ----------------------------------------------------------------
    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "db_path",
                "description": "SQLite file shared with other memware clients",
                "default": _DEFAULT_DB,
                "required": False,
            },
            {
                "key": "prefetch_k",
                "description": "Beliefs injected before each turn",
                "default": "6",
                "required": False,
            },
            {
                "key": "auto_sync",
                "description": "Index every completed turn (true/false)",
                "default": "true",
                "required": False,
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        cfg = Path(hermes_home) / "memware.json"
        data = {
            "db_path": values.get("db_path") or _DEFAULT_DB,
            "prefetch_k": int(values.get("prefetch_k") or 6),
            "auto_sync": str(values.get("auto_sync", "true")).lower() in ("1", "true", "yes"),
        }
        cfg.write_text(json.dumps(data, indent=2) + "\n")

    # -- context ---------------------------------------------------------------
    def system_prompt_block(self) -> str:
        return (
            "Memory: you have a memware ledger and transcript index. Facts injected as "
            "'Known facts' are currently valid; if you learn one has changed, call "
            "memware_remember with the new value. For anything about prior sessions, call "
            "memware_recall before answering."
        )

    def prefetch(self, query: str, **kwargs: Any) -> str:
        if not query or not query.strip():
            return ""
        from memware.index import search_beliefs
        from memware.store import Store

        try:
            with Store(self._db) as s:
                hits = search_beliefs(s, query, k=self._prefetch_k, require_subject=True)
        except Exception as e:  # never break a turn over memory
            logger.warning("memware prefetch failed: %s", e)
            return ""
        if not hits:
            return ""
        lines = []
        for h in hits:
            value = h.text.removeprefix(f"{h.subject} {h.relation} ")
            since = f" (since {h.ts[:10]})" if h.ts else ""
            lines.append(f"- {h.subject} {h.relation}: {value}{since}")
        return "Known facts (currently valid, from the memory ledger):\n" + "\n".join(lines)

    # -- capture ---------------------------------------------------------------
    def _session_file(self, session_id: str) -> Path:
        d = self._home / "memware" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{session_id or self._session_id or 'hermes'}.jsonl"

    def _append_and_index(self, session_id: str, pairs: list[tuple[str, str]]) -> None:
        from memware.ingest import sync_file
        from memware.store import Store, now_iso

        path = self._session_file(session_id)
        with self._lock:
            with path.open("a", encoding="utf-8") as fh:
                for role, content in pairs:
                    if content and content.strip():
                        fh.write(
                            json.dumps(
                                {
                                    "role": role,
                                    "content": content,
                                    "timestamp": now_iso(),
                                    "session": path.stem,
                                }
                            )
                            + "\n"
                        )
            with Store(self._db) as s:
                sync_file(s, path, harness="generic")

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        if not self._auto_sync:
            return
        pairs = [("user", _text(user_content)), ("assistant", _text(assistant_content))]

        def _run() -> None:
            try:
                self._append_and_index(session_id, pairs)
            except Exception as e:
                logger.warning("memware sync_turn failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(target=_run, daemon=True)
        self._sync_thread.start()

    def on_session_end(self, messages: list[dict[str, Any]], **kwargs: Any) -> None:
        self.shutdown()
        # turns were captured incrementally; re-sync the file once for anything missed
        try:
            from memware.ingest import sync_file
            from memware.store import Store

            path = self._session_file(kwargs.get("session_id", "") or self._session_id)
            if path.exists():
                with Store(self._db) as s:
                    sync_file(s, path, harness="generic")
        except Exception as e:
            logger.warning("memware on_session_end failed: %s", e)

    def on_pre_compress(self, messages: list[dict[str, Any]], **kwargs: Any) -> None:
        self.on_session_end(messages, **kwargs)

    def on_memory_write(self, action: str, target: str, content: str, **kwargs: Any) -> None:
        """Mirror built-in memory adds as human-stated beliefs (reliability 0.9)."""
        if action not in ("add", "append", "create") or not content or not content.strip():
            return
        try:
            from memware.ledger import assert_belief
            from memware.store import Store

            with Store(self._db) as s:
                assert_belief(
                    s,
                    target or "hermes memory",
                    "note",
                    content.strip(),
                    reliability=0.9,
                    source=f"hermes built-in memory ({action})",
                )
        except Exception as e:
            logger.warning("memware on_memory_write failed: %s", e)

    # -- tools -----------------------------------------------------------------
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return _TOOLS

    def handle_tool_call(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        from memware.index import read_turns, search_beliefs, search_turns
        from memware.ledger import Policy, assert_belief, current
        from memware.store import Store

        try:
            with Store(self._db) as s:
                if name == "memware_recall":
                    k = int(args.get("k", 8))
                    what = args.get("what", "all")
                    hits = []
                    if what in ("all", "beliefs"):
                        hits += search_beliefs(s, args["query"], k=k)
                    if what in ("all", "turns"):
                        hits += search_turns(s, args["query"], k=k)
                    return json.dumps(
                        [
                            {
                                "kind": h.kind,
                                "id": h.id,
                                "session": h.session,
                                "ts": h.ts,
                                "role": h.role,
                                "subject": h.subject,
                                "relation": h.relation,
                                "text": (h.snippet or h.text)[:600],
                            }
                            for h in hits
                        ]
                    )
                if name == "memware_read_session":
                    return json.dumps(
                        read_turns(
                            s,
                            args["session"],
                            around=args.get("around"),
                            window=int(args.get("window", 5)),
                        )
                    )
                if name == "memware_remember":
                    r = assert_belief(
                        s,
                        args["subject"],
                        args["relation"],
                        args["value"],
                        source=args.get("source"),
                        reliability=float(args.get("reliability", 0.7)),
                        policy=Policy.GATE_CONFLICTS,
                    )
                    return json.dumps(
                        {
                            "outcome": r.outcome.value,
                            "belief_id": r.belief_id,
                            "review_id": r.review_id,
                        }
                    )
                if name == "memware_beliefs":
                    return json.dumps(current(s, args.get("subject")))
        except Exception as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"unknown tool {name}"})


def register(ctx: Any) -> None:
    """Called by Hermes's memory plugin discovery."""
    ctx.register_memory_provider(MemwareProvider())
