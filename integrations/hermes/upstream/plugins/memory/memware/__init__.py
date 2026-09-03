"""memware memory provider — transcript recall plus a belief ledger.

memware keeps two things in one SQLite file: an index of past conversation
turns, and a bi-temporal *belief ledger* whose reads only ever return the
currently valid value for a fact. When a fact changes, the old value is closed
out rather than deleted, so the prompt never carries a superseded value while
the history stays auditable.

Everything is local — no account, no network, no credentials. The store is a
single file, so pointing several clients (Hermes, the ``memware`` CLI, an MCP
server) at one path makes them remember the same things.

Design notes for reviewers:

* ``prefetch`` injects **beliefs only** — small, bounded, and always current.
  Transcript search is on demand through the ``memware_recall`` tool, so an
  ordinary turn costs one indexed FTS query and no LLM call.
* ``sync_turn`` appends the completed turn to a per-session JSONL file under
  ``<hermes_home>/memware/sessions/`` and indexes it from a byte-offset cursor
  on a daemon thread, per the threading contract.
* Storage is profile-scoped by default (``$HERMES_HOME/memware/memware.db``).
  Users who want one store shared with other memware clients set ``db_path``
  to ``~/.memware/memware.db``; ``backup_paths()`` then declares it so
  ``hermes backup`` captures it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_LAZY_FEATURE = "memory.memware"
_DEFAULT_DB = "$HERMES_HOME/memware/memware.db"
_CONFIG_FILE = "memware.json"

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "memware_recall",
        "description": (
            "Search past sessions and currently valid beliefs. Returns ranked, dated "
            "snippets. Call it before answering anything about prior work; call again "
            "with different words if the first hits are not it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."},
                "k": {"type": "integer", "default": 8, "description": "Max hits."},
                "what": {
                    "type": "string",
                    "enum": ["all", "turns", "beliefs"],
                    "default": "all",
                    "description": "Search transcripts, the belief ledger, or both.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memware_read_session",
        "description": (
            "Read a past session's turns in order, or a window around one turn id "
            "returned by memware_recall."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "Session id."},
                "around": {"type": "integer", "description": "Turn id to centre on."},
                "window": {"type": "integer", "default": 5, "description": "Turns each side."},
            },
            "required": ["session"],
        },
    },
    {
        "name": "memware_remember",
        "description": (
            "Record a fact as (subject, relation, value). A new value for the same "
            "subject and relation supersedes the old one; use it the moment you learn "
            "a previously known fact has changed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "What the fact is about."},
                "relation": {"type": "string", "description": "Which property of it."},
                "value": {"type": "string", "description": "The value that now holds."},
                "source": {"type": "string", "description": "Where it came from."},
                "reliability": {
                    "type": "number",
                    "default": 0.7,
                    "description": "0-1. Higher for facts a human stated.",
                },
            },
            "required": ["subject", "relation", "value"],
        },
    },
    {
        "name": "memware_beliefs",
        "description": "List currently valid beliefs, optionally for one subject.",
        "parameters": {
            "type": "object",
            "properties": {"subject": {"type": "string", "description": "Filter by subject."}},
        },
    },
]


def _ensure_memware() -> None:
    """Install the ``memware`` package on first use.

    Single chokepoint: every import of ``memware.*`` in this module goes
    through a method that has called this first. Keeping it out of
    ``is_available()`` is deliberate — gating availability on the package
    being importable would stop the provider loading on a sealed venv, so
    ``initialize()`` (and therefore this install) would never run.
    """
    from tools.lazy_deps import ensure

    ensure(_LAZY_FEATURE, prompt=False)


def _expand(path: str, hermes_home: str) -> str:
    """Resolve ``$HERMES_HOME`` and ``~`` in a user-supplied path."""
    return os.path.expanduser(
        path.replace("${HERMES_HOME}", hermes_home).replace("$HERMES_HOME", hermes_home)
    )


def _text(content: Any) -> str:
    """Flatten a message content field to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _read_config(hermes_home: str) -> dict[str, Any]:
    path = Path(hermes_home) / _CONFIG_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("Failed to parse %s", path, exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class MemwareMemoryProvider(MemoryProvider):
    """Local belief ledger + transcript index, in one SQLite file."""

    def __init__(self) -> None:
        self._db = ""
        self._home = Path(".")
        self._session_id = ""
        self._prefetch_k = 6
        self._auto_sync = True
        self._lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None

    # ── identity / lifecycle ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "memware"

    def is_available(self) -> bool:
        """Always available: local SQLite, no account, no credentials.

        Deliberately does not import ``memware`` — see ``_ensure_memware``.
        """
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        from hermes_constants import get_hermes_home

        home = kwargs.get("hermes_home") or str(get_hermes_home())
        self._home = Path(home)
        self._session_id = session_id or "hermes"

        config = _read_config(home)
        self._db = _expand(str(config.get("db_path") or _DEFAULT_DB), home)
        self._prefetch_k = int(config.get("prefetch_k") or 6)
        self._auto_sync = _as_bool(config.get("auto_sync"), True)

        _ensure_memware()
        from memware.store import Store

        Path(self._db).parent.mkdir(parents=True, exist_ok=True)
        Store(self._db).close()  # create the schema eagerly so the first prefetch is cheap

    def shutdown(self) -> None:
        self._join_sync()

    def _join_sync(self) -> None:
        thread = self._sync_thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)

    # ── config ──────────────────────────────────────────────────────────────

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "db_path",
                "description": (
                    "SQLite file. Defaults to this profile; set it to "
                    "~/.memware/memware.db to share one store with other memware clients"
                ),
                "default": _DEFAULT_DB,
            },
            {
                "key": "prefetch_k",
                "description": "Currently valid beliefs injected before each turn",
                "default": 6,
                "type": "integer",
                "minimum": 0,
                "maximum": 50,
            },
            {
                "key": "auto_sync",
                "description": "Index every completed turn for later recall",
                "default": True,
                "type": "boolean",
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        config = _read_config(hermes_home)
        config.update(
            {
                "db_path": str(values.get("db_path") or config.get("db_path") or _DEFAULT_DB),
                "prefetch_k": int(values.get("prefetch_k") or config.get("prefetch_k") or 6),
                "auto_sync": _as_bool(values.get("auto_sync"), True),
            }
        )
        path = Path(hermes_home) / _CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def backup_paths(self) -> list[str]:
        """Declare the store when the user has pointed it outside HERMES_HOME."""
        try:
            from hermes_constants import get_hermes_home

            home = str(get_hermes_home())
            db = self._db or _expand(str(_read_config(home).get("db_path") or _DEFAULT_DB), home)
            if Path(db).is_relative_to(Path(home)):
                return []  # already inside HERMES_HOME; `hermes backup` walks it
            return [db]
        except Exception:
            logger.debug("memware: could not resolve backup paths", exc_info=True)
            return []

    # ── context ─────────────────────────────────────────────────────────────

    def system_prompt_block(self) -> str:
        return (
            "Memory: you have a memware belief ledger and transcript index. Facts "
            "injected as 'Known facts' are currently valid; if you learn one has "
            "changed, call memware_remember with the new value. For anything about "
            "prior sessions, call memware_recall before answering."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or not query.strip() or self._prefetch_k <= 0:
            return ""
        try:
            _ensure_memware()
            from memware.index import search_beliefs
            from memware.store import Store

            with Store(self._db) as store:
                hits = search_beliefs(store, query, k=self._prefetch_k)
        except Exception as e:  # never break a turn over memory
            logger.warning("memware prefetch failed: %s", e)
            return ""
        if not hits:
            return ""
        lines = []
        for hit in hits:
            value = hit.text.removeprefix(f"{hit.subject} {hit.relation} ")
            since = f" (since {hit.ts[:10]})" if hit.ts else ""
            lines.append(f"- {hit.subject} {hit.relation}: {value}{since}")
        return "Known facts (currently valid, from the memware ledger):\n" + "\n".join(lines)

    # ── capture ─────────────────────────────────────────────────────────────

    def _session_file(self, session_id: str = "") -> Path:
        directory = self._home / "memware" / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{session_id or self._session_id or 'hermes'}.jsonl"

    def _append_and_index(self, session_id: str, pairs: list[tuple]) -> None:
        _ensure_memware()
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
            with Store(self._db) as store:
                sync_file(store, path, harness="generic")

    def _flush(self, session_id: str = "") -> None:
        """Re-index the session file from its cursor. Idempotent."""
        try:
            _ensure_memware()
            from memware.ingest import sync_file
            from memware.store import Store

            path = self._session_file(session_id)
            if path.exists():
                with self._lock, Store(self._db) as store:
                    sync_file(store, path, harness="generic")
        except Exception as e:
            logger.warning("memware flush failed: %s", e)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Append and index the completed turn. Non-blocking, per the contract.

        Only the user and assistant text is stored — never ``messages``, which
        can carry tool arguments and command output. Nothing leaves the device
        either way.
        """
        if not self._auto_sync:
            return
        pairs = [("user", _text(user_content)), ("assistant", _text(assistant_content))]
        target = session_id or self._session_id

        def _run() -> None:
            try:
                self._append_and_index(target, pairs)
            except Exception as e:
                logger.warning("memware sync_turn failed: %s", e)

        self._join_sync()
        self._sync_thread = threading.Thread(target=_run, daemon=True, name="memware-sync")
        self._sync_thread.start()

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        self._join_sync()
        self._flush()

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs: Any,
    ) -> None:
        """Land any in-flight writes in the outgoing session, then rebind."""
        self._join_sync()
        self._flush()
        self._session_id = new_session_id or self._session_id

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Flush before the transcript is discarded.

        Contributes nothing to the compression summary: what matters is already
        in the ledger, and ``prefetch`` re-injects it on the next turn.
        """
        self._join_sync()
        self._flush()
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mirror built-in memory adds into the ledger as human-stated beliefs."""
        if action not in ("add", "append", "create") or not content or not content.strip():
            return
        try:
            _ensure_memware()
            from memware.ledger import assert_belief
            from memware.store import Store

            with Store(self._db) as store:
                assert_belief(
                    store,
                    target or "hermes memory",
                    "note",
                    content.strip(),
                    reliability=0.9,
                    source=f"hermes built-in memory ({action})",
                )
        except Exception as e:
            logger.warning("memware on_memory_write failed: %s", e)

    # ── tools ───────────────────────────────────────────────────────────────

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return _TOOLS

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        try:
            _ensure_memware()
            from memware.index import read_turns, search_beliefs, search_turns
            from memware.ledger import Policy, assert_belief, current
            from memware.store import Store

            with Store(self._db) as store:
                if tool_name == "memware_recall":
                    k = int(args.get("k", 8))
                    what = args.get("what", "all")
                    hits = []
                    if what in ("all", "beliefs"):
                        hits += search_beliefs(store, args["query"], k=k)
                    if what in ("all", "turns"):
                        hits += search_turns(store, args["query"], k=k)
                    return json.dumps(
                        [
                            {
                                "kind": hit.kind,
                                "id": hit.id,
                                "session": hit.session,
                                "ts": hit.ts,
                                "role": hit.role,
                                "subject": hit.subject,
                                "relation": hit.relation,
                                "text": (hit.snippet or hit.text)[:600],
                            }
                            for hit in hits
                        ]
                    )
                if tool_name == "memware_read_session":
                    return json.dumps(
                        read_turns(
                            store,
                            args["session"],
                            around=args.get("around"),
                            window=int(args.get("window", 5)),
                        )
                    )
                if tool_name == "memware_remember":
                    result = assert_belief(
                        store,
                        args["subject"],
                        args["relation"],
                        args["value"],
                        source=args.get("source"),
                        reliability=float(args.get("reliability", 0.7)),
                        policy=Policy.GATE_CONFLICTS,
                    )
                    return json.dumps(
                        {
                            "outcome": result.outcome.value,
                            "belief_id": result.belief_id,
                            "review_id": result.review_id,
                        }
                    )
                if tool_name == "memware_beliefs":
                    return json.dumps(current(store, args.get("subject")))
        except Exception as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"unknown tool {tool_name}"})


def register(ctx: Any) -> None:
    """Entry point for the memory plugin discovery system."""
    ctx.register_memory_provider(MemwareMemoryProvider())
