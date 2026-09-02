"""Parser for plain message JSONL: one object per line with ``role``, ``content``
and optional ``timestamp``/``session`` fields. Many harnesses can export this."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from memware.ingest import Turn, register

MIN_CHARS = 20
MAX_CHARS = 20_000


def parse(path: Path, start: int = 0) -> Iterator[tuple[int, Turn]]:
    fallback_session = path.stem
    with path.open("rb") as fh:
        fh.seek(start)
        pos = start
        for raw in fh:
            pos += len(raw)
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role = d.get("role")
            if role not in ("user", "assistant"):
                continue
            content = d.get("content")
            if isinstance(content, str):
                text = content
            else:
                text = " ".join(
                    str(b.get("text", "")) for b in content or [] if isinstance(b, dict)
                )
            t = text.strip()
            if len(t) < MIN_CHARS:
                continue
            yield (
                pos,
                Turn(
                    session=str(d.get("session") or d.get("session_id") or fallback_session),
                    ts=d.get("timestamp") or d.get("ts"),
                    role=role,
                    text=t[:MAX_CHARS],
                ),
            )


register("generic", parse)
