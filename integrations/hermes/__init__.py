"""memware memory provider for Hermes Agent — experimental skeleton.

Implements the provider hooks by shelling out to the ``memware`` CLI so the
plugin has no import-time dependency on memware internals. Tracks Hermes's
provider interface loosely; adjust method names to your Hermes version.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any


def _memware(*args: str, stdin: str | None = None) -> str:
    env = dict(os.environ)
    proc = subprocess.run(
        ["memware", *args], input=stdin, capture_output=True, text=True, env=env, timeout=30
    )
    return proc.stdout


class MemwareProvider:
    name = "memware"

    def initialize(self, config: dict[str, Any] | None = None) -> None:
        self.k = int((config or {}).get("k", 6))

    def is_available(self) -> bool:
        try:
            _memware("--version")
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def prefetch(self, prompt: str, **_: Any) -> str:
        """Currently valid beliefs relevant to the prompt, as a context block."""
        return _memware("context", "-k", str(self.k), prompt).strip()

    def system_prompt_block(self, **_: Any) -> str:
        return (
            "You have a memory ledger. When you learn that a previously known fact has "
            "changed, record the new value with `memware assert <subject> <relation> <value>`."
        )

    def on_session_end(self, messages: list[dict[str, Any]], **_: Any) -> None:
        """Index the session as generic message JSONL."""
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for m in messages:
                if m.get("role") in ("user", "assistant"):
                    fh.write(json.dumps({"role": m["role"], "content": m.get("content", ""),
                                         "timestamp": m.get("timestamp")}) + "\n")
            path = fh.name
        try:
            _memware("sync", path, "--harness", "generic")
        finally:
            os.unlink(path)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {"name": "memware_recall", "description": "Search past sessions and current beliefs.",
             "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                            "required": ["query"]}},
            {"name": "memware_remember",
             "description": "Record a fact; a new value supersedes the old one for the same subject and relation.",
             "parameters": {"type": "object",
                            "properties": {"subject": {"type": "string"}, "relation": {"type": "string"},
                                           "value": {"type": "string"}, "source": {"type": "string"}},
                            "required": ["subject", "relation", "value"]}},
        ]

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> str:
        if name == "memware_recall":
            return _memware("recall", args["query"], "--json")
        if name == "memware_remember":
            extra = ["--source", args["source"]] if args.get("source") else []
            return _memware("assert", args["subject"], args["relation"], args["value"], *extra, "--json")
        return json.dumps({"error": f"unknown tool {name}"})
