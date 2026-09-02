"""Parser for Claude Code session transcripts (``~/.claude/projects/**/*.jsonl``).

Keeps human prompts and assistant prose. Skips tool calls/results and drops
text the harness injected rather than a person wrote (blocks that start with
``<``, e.g. system reminders). Nothing here calls a model.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from memware.ingest import Turn, register

MIN_CHARS = 20
MAX_CHARS = 20_000
SKIP_PREFIXES: tuple[str, ...] = ("<",)
_MARKERS = (b'"type":"user"', b'"type":"assistant"', b'"type": "user"', b'"type": "assistant"')


def _texts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(str(block.get("text", "")))
    return out


def parse(path: Path, start: int = 0) -> Iterator[tuple[int, Turn]]:
    fallback_session = path.stem
    with path.open("rb") as fh:
        fh.seek(start)
        pos = start
        for raw in fh:
            pos += len(raw)
            if not any(m in raw for m in _MARKERS):
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role = d.get("type")
            if role not in ("user", "assistant"):
                continue
            session = str(d.get("sessionId") or fallback_session)
            ts = d.get("timestamp")
            for text in _texts((d.get("message") or {}).get("content")):
                t = text.strip()
                if len(t) < MIN_CHARS or (role == "user" and t.startswith(SKIP_PREFIXES)):
                    continue
                yield pos, Turn(session=session, ts=ts, role=role, text=t[:MAX_CHARS])


register("claude-code", parse)
