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


def write_hook_transcript(
    path: Path,
    session: str,
    prompts: list[tuple[str, str, list[str]]],
    *,
    entrypoint: str = "cli",
    sidechain: bool = False,
) -> None:
    """Claude Code-shaped JSONL for sessions where the prompt hook injected facts.

    prompts: (ts, typed prompt, injected lines). Each prompt is written as Claude Code writes
    one: the typed user record, a meta user record and an unrelated attachment between it and
    the hook's ``hook_additional_context`` attachment (whose parent chain leads back to the
    prompt), another plugin's context on the same event, and a tool hook after. An empty list of
    lines writes no memware block at all."""
    from memware.digest import CONTEXT_TITLE

    lines: list[dict[str, object]] = [{"type": "summary", "summary": "noise"}]
    base = {"sessionId": session, "entrypoint": entrypoint, "isSidechain": sidechain}
    hook = {
        "type": "hook_additional_context",
        "hookName": "UserPromptSubmit",
        "hookEvent": "UserPromptSubmit",
    }
    parent: str | None = None
    for i, (ts, prompt, injected) in enumerate(prompts):
        ids = [f"{session}-{i}-{n}" for n in range(6)]
        typed = {"role": "user", "content": prompt}
        meta = {"role": "user", "content": [{"type": "text", "text": "meta"}]}
        records = [
            {"type": "user", "uuid": ids[0], "parentUuid": parent, "message": typed},
            {"type": "user", "uuid": ids[1], "parentUuid": ids[0], "isMeta": True, "message": meta},
            {
                "type": "attachment",
                "uuid": ids[2],
                "parentUuid": ids[1],
                "attachment": {"type": "date"},
            },
            {
                "type": "attachment",
                "uuid": ids[3],
                "parentUuid": ids[2],
                "attachment": hook | {"content": ["Another plugin's context\n- not a fact"]},
            },
        ]
        if injected:
            block = CONTEXT_TITLE + "\n" + "\n".join(injected)
            records.append(
                {
                    "type": "attachment",
                    "uuid": ids[4],
                    "parentUuid": ids[3],
                    "attachment": hook | {"content": [block]},
                }
            )
        tool_hook = {
            "type": "hook_success",
            "hookName": "PreToolUse:Bash",
            "hookEvent": "PreToolUse",
        }
        records.append(
            {
                "type": "attachment",
                "uuid": ids[5],
                "parentUuid": records[-1]["uuid"],
                "attachment": tool_hook,
            }
        )
        lines += [base | {"timestamp": ts} | r for r in records]
        parent = ids[5]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")


def write_claude_jsonl(
    path: Path,
    session: str,
    turns: list[tuple[str, str, str]],
    *,
    entrypoint: str | None = None,
) -> None:
    """turns: (role, ts, text). Writes Claude Code-shaped JSONL with a few noise rows. With
    ``entrypoint``, every conversation record carries it, as Claude Code writes them; without,
    the records have no such field, as older versions wrote them."""
    stamp = {"entrypoint": entrypoint} if entrypoint else {}
    lines = [json.dumps({"type": "summary", "summary": "noise"})]
    for role, ts, text in turns:
        content = text if role == "user" else [{"type": "text", "text": text}]
        lines.append(
            json.dumps(
                {
                    "type": role,
                    "sessionId": session,
                    "timestamp": ts,
                    **stamp,
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
                        **stamp,
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "tool_use", "name": "Read", "input": {}}],
                        },
                    }
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
