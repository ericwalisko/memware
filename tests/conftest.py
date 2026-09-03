import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from memware.store import Store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real ``~/.memware`` out of the suite.

    ``ignore-markers.txt`` there would otherwise decide what a test is allowed to
    index, so results would depend on the machine. Tests that want markers set
    ``MEMWARE_HOME`` themselves, which lands after this fixture.
    """
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "memware-home"))
    for var in ("MEMWARE_IGNORE_MARKERS", "MEMWARE_NO_CAPTURE", "MEMWARE_DB"):
        monkeypatch.delenv(var, raising=False)


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
