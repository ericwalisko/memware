"""Unit tests for the recall-election harness (parser, fatal detection, loaders, scoring).

Not collected by the package's pytest config (testpaths = tests); run explicitly:

    uv run --extra dev pytest eval/recall_election/test_harness.py -q

SAMPLE is one real ``claude -p --output-format stream-json`` run (sonnet, dev scenario,
2026-09-10) with thinking and tool-result payloads trimmed; event shapes are verbatim.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe
import run
import score

SAMPLE = r"""
{"type": "system", "subtype": "init", "tools": ["Glob", "Grep", "Read", "mcp__memware__beliefs", "mcp__memware__read_session", "mcp__memware__recall"], "mcp_servers": [{"name": "memware", "status": "connected"}], "model": "claude-sonnet-5"}
{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}}
{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]}, "parent_tool_use_id": null}
{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_01PYMKk2t1rFUJWELZJxyAZn", "name": "mcp__memware__recall", "input": {"queries": ["sidecar port 8071", "port 8070 conflict", "sidecar port choice reason", "why port 8071 instead of 8070"]}}]}, "parent_tool_use_id": null}
{"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01PYMKk2t1rFUJWELZJxyAZn", "content": "[3 hits]"}]}}
{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "8070 collided with the metrics exporter on the shared host, so you moved the sidecar to 8071. That decision was documented in the runbook but never made it into the code itself."}]}, "parent_tool_use_id": null}
{"type": "result", "subtype": "success", "is_error": false, "num_turns": 2, "result": "8070 collided with the metrics exporter on the shared host, so you moved the sidecar to 8071. That decision was documented in the runbook but never made it into the code itself.", "permission_denials": [], "duration_ms": 3552}
"""


def test_parse_real_stream() -> None:
    p = run.parse_stream(SAMPLE)
    assert p["tools_called"] == ["mcp__memware__recall"]
    assert p["first_tool"] == "mcp__memware__recall"
    assert p["recall_called"] is True
    assert p["n_phrasings"] == 4
    assert p["recall_queries"][0] == "sidecar port 8071"
    assert p["turns"] == 2
    assert p["is_error"] is False
    assert p["result_subtype"] == "success"
    assert p["permission_denials"] == 0
    assert p["rate_limit_status"] == "allowed_warning"
    assert p["init_tools"] == [
        "Glob",
        "Grep",
        "Read",
        "mcp__memware__beliefs",
        "mcp__memware__read_session",
        "mcp__memware__recall",
    ]
    assert p["init_mcp_servers"] == [{"name": "memware", "status": "connected"}]
    assert p["final_text"].startswith("8070 collided")
    assert len(p["final_text"]) <= run.FINAL_TEXT_CHARS


def test_parse_ignores_noise_and_subagents() -> None:
    lines = [
        "not json at all",
        json.dumps(
            {
                "type": "assistant",
                "parent_tool_use_id": "t1",
                "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}]
                },
            }
        ),
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done " * 200}]}}
        ),
    ]
    p = run.parse_stream("\n".join(lines))
    assert p["tools_called"] == ["Grep"]
    assert p["recall_called"] is False
    assert p["n_phrasings"] == 0
    assert p["turns"] == 2  # no result event: assistant messages counted
    assert len(p["final_text"]) == run.FINAL_TEXT_CHARS


def test_fatal_reason() -> None:
    ok = {"rc": 0, "stdout": SAMPLE, "stderr": ""}
    assert run.fatal_reason(ok, run.parse_stream(SAMPLE)) is None  # allowed_warning is fine
    limited = {"rc": 1, "stdout": "", "stderr": "You've hit your session limit, resets at 3pm"}
    assert run.fatal_reason(limited, run.parse_stream("")).startswith("usage limit")
    auth = {"rc": 1, "stdout": "", "stderr": "Not logged in. Please run /login"}
    assert run.fatal_reason(auth, run.parse_stream("")).startswith("auth")
    rejected = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
    assert run.fatal_reason(
        {"rc": 1, "stdout": rejected, "stderr": ""}, run.parse_stream(rejected)
    ).startswith("usage limit")
    # A non-zero exit whose only "rate_limit" text is the stream's own allowed event is not fatal.
    crashed = {"rc": 1, "stdout": SAMPLE, "stderr": "boom"}
    assert run.fatal_reason(crashed, run.parse_stream(SAMPLE)) is None
    # The model's own answer is not scanned on success: the fixture is an auth gateway.
    answer = "Replace HTTPStatus.UNAUTHORIZED (401) with the helper; rate limit the retries."
    talky = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": answer}
    )
    assert (
        run.fatal_reason({"rc": 0, "stdout": talky, "stderr": ""}, run.parse_stream(talky)) is None
    )
    # ...but it is scanned when the result itself is an error.
    denied = json.dumps(
        {"type": "result", "subtype": "error", "is_error": True, "result": "Not logged in"}
    )
    assert run.fatal_reason(
        {"rc": 0, "stdout": denied, "stderr": ""}, run.parse_stream(denied)
    ).startswith("auth")


def test_build_argv_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(run, "supports_flag", lambda flag: flag == "--append-system-prompt")
    cfg = tmp_path / "mcp.json"
    argv = run.build_argv("hi", "opus", cfg, "both")
    assert argv[:3] == ["/usr/bin/claude", "-p", "hi"]
    assert "--bare" not in argv
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
    assert argv[argv.index("--append-system-prompt") + 1] == run.MARKER
    assert argv[argv.index("--tools") + 1] == "Grep,Read,Glob"
    only_sources = run.build_argv("hi", "opus", cfg, "setting-sources")
    assert "--settings" not in only_sources
    with pytest.raises(ValueError):
        run.build_argv("hi", "opus", cfg, "nope")


def test_mcp_config_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = json.loads(
        run.write_mcp_config(tmp_path, tmp_path / "v.md", tmp_path / "log").read_text()
    )
    srv = cfg["mcpServers"]["memware"]
    assert srv["command"] == sys.executable
    assert srv["args"] == [str(run.STUB_SERVER)]
    assert srv["env"]["RECALL_DESCRIPTION_FILE"].endswith("v.md")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    env = run.child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert env["MEMWARE_NO_CAPTURE"] == "1"


def test_load_scenarios(tmp_path: Path) -> None:
    f = tmp_path / "s.json"
    f.write_text(
        json.dumps(
            {
                "scenarios": [
                    {"id": "a", "class": "decision", "expect": True, "prompt": "why?"},
                    {"name": "b", "kind": "in-tree", "expect": "no", "question": "what?"},
                ]
            }
        )
    )
    s = run.load_scenarios(f)
    assert [x["id"] for x in s] == ["a", "b"]
    assert s[0]["expect"] is True and s[1]["expect"] is False
    assert s[1]["class"] == "in-tree" and s[1]["prompt"] == "what?"
    f.write_text(json.dumps([{"id": "c", "prompt": "p"}]))
    with pytest.raises(SystemExit):
        run.load_scenarios(f)


def test_resume_keys(tmp_path: Path) -> None:
    out = tmp_path / "r.jsonl"
    out.write_text(
        json.dumps({"variant": "v", "scenario": "s", "model": "m", "repeat": 0}) + "\ngarbage\n"
    )
    assert run.load_existing(out) == {("v", "s", "m", 0)}
    assert run.load_existing(tmp_path / "missing.jsonl") == set()


def test_probe_checks() -> None:
    good = (
        "Glob\nGrep\nRead\nmcp__memware__beliefs\nmcp__memware__read_session\nmcp__memware__recall"
    )
    ok, ev = probe.check_tools(good, good.split())
    assert ok and ev["listed_other_mcp"] == []
    leaked = good + "\nmcp__memware__remember\nmcp__other__x"
    ok, ev = probe.check_tools(leaked, [])
    assert not ok and ev["listed_other_mcp"] == ["mcp__other__x"]
    ok, _ = probe.check_tools(good, [*good.split(), "mcp__memware__pending_reviews"])
    assert not ok  # init event disagrees with the answer
    assert probe.check_known_facts("NO")[0] and probe.check_known_facts("no.")[0]
    assert not probe.check_known_facts("YES")[0] and not probe.check_known_facts("")[0]


def test_wilson_and_summary() -> None:
    lo, hi = score.wilson(8, 10)
    assert 0.49 < lo < 0.50 and 0.94 < hi < 0.95  # 0.8 -> [0.490, 0.943]
    assert all(score.math.isnan(x) for x in score.wilson(0, 0))
    rows = [
        {"expect": True, "recall_called": True, "first_tool": score.RECALL, "n_phrasings": 4},
        {"expect": True, "recall_called": False, "first_tool": "Grep", "n_phrasings": 0},
        {"expect": False, "recall_called": True, "first_tool": score.RECALL, "n_phrasings": 2},
        {"expect": False, "recall_called": False, "first_tool": "Read", "n_phrasings": 0},
        {"expect": False, "recall_called": False, "first_tool": None, "n_phrasings": 0},
    ]
    s = score.summarise_group(rows)
    assert (s["tpr"], s["fpr"]) == (0.5, 1 / 3)
    assert abs(s["bal_acc"] - (0.5 + 2 / 3) / 2) < 1e-9
    assert s["mean_phrasings"] == 3.0 and s["first_recall_rate"] == 0.5


def test_report_flags_no_signal(tmp_path: Path) -> None:
    rows = []
    for v in ("a", "b"):
        rows.append(
            {
                "variant": v,
                "model": "m",
                "scenario": "always",
                "class": "c",
                "expect": True,
                "recall_called": True,
                "first_tool": score.RECALL,
                "n_phrasings": 3,
                "valid": True,
            }
        )
        rows.append(
            {
                "variant": v,
                "model": "m",
                "scenario": "split",
                "class": "d",
                "expect": False,
                "recall_called": v == "a",
                "first_tool": None,
                "n_phrasings": 0,
                "valid": True,
            }
        )
    rows.append(
        {
            "variant": "a",
            "model": "m",
            "scenario": "split",
            "class": "d",
            "expect": False,
            "recall_called": False,
            "error": "timeout after 150s",
            "valid": False,
        }
    )
    report = score.build_report(rows, tmp_path / "r.jsonl")
    assert "no signal (all 1)" in report
    assert report.count("no signal") == 2  # one flagged row plus the explanatory sentence
    assert "1 invalid (excluded)" in report
    assert "| a | m | 2 | 1/1 |" in report
