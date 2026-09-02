import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from memware.store import Store


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def write_claude_jsonl(path: Path, session: str, turns: list[tuple[str, str, str]]) -> None:
    """turns: (role, ts, text). Writes Claude Code-shaped JSONL with a few noise rows."""
    lines = [json.dumps({"type": "summary", "summary": "noise"})]
    for role, ts, text in turns:
        content = text if role == "user" else [{"type": "text", "text": text}]
        lines.append(
            json.dumps(
                {
                    "type": role,
                    "sessionId": session,
                    "timestamp": ts,
                    "message": {"role": role, "content": content},
                }
            )
        )
        if role == "assistant":
            lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": session,
                        "timestamp": ts,
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "tool_use", "name": "Read", "input": {}}],
                        },
                    }
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
