"""Unit tests for the recall-election harness (parser, isolation, fatal detection, loaders,
scoring, the sign test and the decision).

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
    assert p["tool_inputs"] == [{"name": "mcp__memware__recall"}]  # queries are not path inputs
    assert p["recall_count"] == 1
    assert p["has_init"] is True and p["memory_paths"] == {}


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
    assert "--append-system-prompt" not in argv  # no marker unless asked: it names the eval
    marked = run.build_argv("hi", "opus", cfg, "both", marker=True)
    assert marked[marked.index("--append-system-prompt") + 1] == run.MARKER
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
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


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
    assert probe.check_project_answer("An HTTP gateway that verifies HMAC-signed requests.")[0]
    ok, ev = probe.check_project_answer("A test Fixture project for an eval Harness.")
    assert not ok and ev["banned_words"] == ["eval", "fixture", "harness"]
    assert not probe.check_project_answer("  ")[0]  # no answer proves nothing


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
    # Phrasings over positives only: the negative's 2-phrasing false call is left out.
    assert s["mean_phrasings"] == 4.0 and s["n_recall"] == 1 and s["first_recall_rate"] == 0.5


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


# ------------------------------------------------------------------------------ isolation


def _stream(*tool_uses: tuple[str, dict[str, object]]) -> str:
    """A minimal stream-json transcript: init, one assistant message per tool use, a result."""
    lines = [
        json.dumps({"type": "system", "subtype": "init", "cwd": "/w/gateway-abc", "tools": []}),
        *(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": n, "input": i}]},
                }
            )
            for n, i in tool_uses
        ),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}),
    ]
    return "\n".join(lines)


CELL = {"variant": "v", "scenario": "s", "class": "c", "expect": True, "model": "m", "repeat": 0}


def _raw(copy: Path, stdout: str, stub_tools: list[str]) -> dict[str, object]:
    return {
        "rc": 0,
        "stdout": stdout,
        "stderr": "",
        "elapsed_s": 1.0,
        "timed_out": False,
        "calls": [{"tool": t} for t in stub_tools],
        "copy_dir": str(copy),
        "dirs_removed": True,
    }


def test_inside_copy(tmp_path: Path) -> None:
    real = tmp_path / "real"
    copy = real / "gateway-abc"
    (copy / "src").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    assert run.inside_copy(str(copy / "src" / "auth.py"), copy)
    assert run.inside_copy("src/../README.md", copy)  # '..' that stays inside
    assert run.inside_copy(".", copy)
    assert run.inside_copy(str(tmp_path / "link" / "gateway-abc" / "README.md"), copy)  # symlink
    assert run.inside_copy(f"{copy}/**/*.py", copy)
    assert run.inside_copy("**/*.py", copy) and run.inside_copy("{src,tests}/**", copy)
    assert not run.inside_copy("../gwmeta-xyz/calls.jsonl", copy)
    assert not run.inside_copy("src/../../x", copy)
    assert not run.inside_copy("src/**/../../x", copy)  # ** may match nothing
    assert not run.inside_copy(f"{copy}/../gwmeta-xyz", copy)
    assert not run.inside_copy("/Users/someone/repo/scenarios.json", copy)
    assert not run.inside_copy(f"{real}/**/mcp.json", copy)
    assert not run.inside_copy(f"{copy}-other/README.md", copy)  # shares the name prefix only
    assert not run.inside_copy("~/.claude/projects", copy)


def test_out_of_copy_invalidates_cell(tmp_path: Path) -> None:
    copy = tmp_path / "gateway-abc"
    copy.mkdir()
    recall = ("mcp__memware__recall", {"queries": ["why 8443"]})
    grep = ("Grep", {"pattern": "/api/v1", "path": "src", "glob": "*.py"})  # regex, not a path
    clean = _stream(recall, grep, ("Read", {"file_path": f"{copy}/README.md"}))
    row = run.build_row(CELL, _raw(copy, clean, ["recall"]), run.parse_stream(clean))
    assert row["valid"] is True and row["invalid_reason"] is None and row["out_of_copy"] == []
    assert row["copy_dir"] == str(copy)
    assert row["tool_inputs"] == [
        {"name": "mcp__memware__recall"},
        {"name": "Grep", "path": "src", "pattern": "/api/v1", "glob": "*.py"},
        {"name": "Read", "file_path": f"{copy}/README.md"},
    ]

    outside = "/Users/someone/repo/eval/scenarios.json"
    dirty = _stream(
        recall,
        grep,
        ("Read", {"file_path": outside}),
        ("Glob", {"pattern": "../*"}),
    )
    row = run.build_row(CELL, _raw(copy, dirty, ["recall"]), run.parse_stream(dirty))
    assert row["valid"] is False and row["invalid_reason"] == "out_of_copy"
    assert row["out_of_copy"] == [f"Read.file_path={outside}", "Glob.pattern=../*"]
    assert row["error"].startswith("out_of_copy: ")
    # It stays out_of_copy even when the spawn also failed.
    failed = {**_raw(copy, dirty, ["recall"]), "rc": 1, "timed_out": True}
    assert run.build_row(CELL, failed, run.parse_stream(dirty))["invalid_reason"] == "out_of_copy"


def test_count_mismatch(tmp_path: Path) -> None:
    recall = ("mcp__memware__recall", {"queries": ["a", "b", "c"]})
    rejected = ("mcp__memware__recall", {"queries": "a"})  # schema-rejected: never reaches the stub
    stream = _stream(rejected, recall)
    row = run.build_row(CELL, _raw(tmp_path, stream, ["recall"]), run.parse_stream(stream))
    assert row["recall_mismatch"] is False  # presence agrees...
    assert (row["recall_count_transcript"], row["recall_count_stub"]) == (2, 1)
    assert row["count_mismatch"] is True  # ...the counts do not
    both = run.build_row(
        CELL, _raw(tmp_path, stream, ["recall", "recall"]), run.parse_stream(stream)
    )
    assert both["count_mismatch"] is False
    none = _stream(("Grep", {"pattern": "x"}))
    row = run.build_row(CELL, _raw(tmp_path, none, ["recall"]), run.parse_stream(none))
    assert row["recall_mismatch"] is True and row["count_mismatch"] is True


def test_cell_dirs(tmp_path: Path) -> None:
    fixture = tmp_path / "proj"
    (fixture / "src" / "__pycache__").mkdir(parents=True)
    (fixture / "README.md").write_text("gateway")
    (fixture / ".env.example").write_text("PORT=8443")
    (fixture / "src" / "app.py").write_text("x = 1")
    (fixture / "src" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"/path/eval/app.py")
    desc = tmp_path / "control.md"
    desc.write_text("Call this when...")
    root = tmp_path / "work"
    with run.cell_dirs(fixture, desc, root) as d:
        assert d.copy.parent == root and d.meta.parent == root
        assert d.copy.name.startswith(run.COPY_PREFIX) and d.meta.name.startswith(run.META_PREFIX)
        assert (d.copy / "README.md").read_text() == "gateway"
        assert (d.copy / ".env.example").exists() and (d.copy / "src" / "app.py").exists()
        assert not (d.copy / "src" / "__pycache__").exists()  # bytecode embeds source paths
        assert d.mcp_config.parent == d.meta and d.call_log.parent == d.meta
        env = json.loads(d.mcp_config.read_text())["mcpServers"]["memware"]["env"]
        assert env["RECALL_CALL_LOG"] == str(d.call_log)
        assert not run.banned_words(str(d.copy)) and run.visible_name_violations(d.copy) == []
        copy, meta = d.copy, d.meta
    assert not copy.exists() and not meta.exists()

    (fixture / "src" / "stub_client.py").write_text("")
    assert run.visible_name_violations(fixture) == ["src/stub_client.py"]
    with pytest.raises(RuntimeError), run.cell_dirs(fixture, desc, root):
        pass
    assert list(root.iterdir()) == []  # removed even when the check fails


def test_preflight(tmp_path: Path) -> None:
    fixture = tmp_path / "proj"
    fixture.mkdir()
    (fixture / "README.md").write_text("gateway")
    run.assert_outside_git(tmp_path)
    with pytest.raises(SystemExit):
        run.assert_outside_git(run.HERE)  # this directory is inside the memware checkout
    run.preflight(fixture, tmp_path / "a" / "work")
    (tmp_path / "a" / "CLAUDE.md").write_text("instructions every cell would load")
    with pytest.raises(SystemExit):
        run.preflight(fixture, tmp_path / "a" / "work")
    with pytest.raises(SystemExit):
        run.preflight(fixture, tmp_path / "eval-work")


def test_memory_path_detection() -> None:
    auto = "/Users/x/.claude/projects/-private-tmp-gateway-work-gateway-abc/memory/"
    init = {"type": "system", "subtype": "init", "memory_paths": {"auto": auto}}
    assert run.memory_path_fields(init) == {"memory_paths.auto": auto}
    assert run.memory_path_fields({"memory_paths": {}}) == {}
    assert run.memory_path_fields({"memory_paths": {"auto": " ", "team": None}}) == {}
    assert run.memory_path_fields({"settings": {"teamMemory": ["/m"]}}) == {
        "settings.teamMemory[0]": "/m"
    }
    assert run.memory_path_fields(None) == {}

    leaked = run.parse_stream(json.dumps(init))
    assert leaked["has_init"] and leaked["memory_paths"] == {"memory_paths.auto": auto}
    ok, ev = probe.check_memory_paths([run.parse_stream(SAMPLE), leaked])
    assert not ok and ev["memory_paths"] == {"spawn 2: memory_paths.auto": auto}
    assert probe.check_memory_paths([run.parse_stream(SAMPLE)])[0]
    ok, ev = probe.check_memory_paths([run.parse_stream("")])
    assert not ok and ev["spawns_without_init"] == [1]  # no init event: cannot pass


# ---------------------------------------------------------------------------- cell order


def test_shuffle_is_deterministic() -> None:
    variants = {v: Path(f"/v/{v}.md") for v in ("control", "grep-contrast", "synthesized")}
    scenarios = [
        {"id": f"s{i}", "prompt": "p", "class": "c", "expect": i % 2 == 0} for i in range(4)
    ]
    cells = run.build_cells(variants, scenarios, ["opus", "sonnet"], 2, seed=20260911)
    keys = [run.cell_key(c) for c in cells]
    assert len(keys) == len(set(keys)) == 3 * 4 * 2 * 2
    assert keys == [
        run.cell_key(c)
        for c in run.build_cells(variants, scenarios, ["opus", "sonnet"], 2, seed=20260911)
    ]
    reordered = dict(reversed(list(variants.items())))
    assert keys == [
        run.cell_key(c)
        for c in run.build_cells(reordered, scenarios, ["sonnet", "opus"], 2, seed=20260911)
    ]
    assert keys != [
        run.cell_key(c) for c in run.build_cells(variants, scenarios, ["opus", "sonnet"], 2, seed=1)
    ]
    assert keys != sorted(keys)
    assert len({k[0] for k in keys[:6]}) > 1  # variants are interleaved, not run in blocks
    done = set(keys[::3])
    assert [k for k in keys if k not in done] == [
        run.cell_key(c) for c in cells if run.cell_key(c) not in done
    ]


def test_select_ids(tmp_path: Path) -> None:
    for v in ("control", "synthesized", "baseline"):
        (tmp_path / f"{v}.md").write_text(v)
    assert run.parse_ids(" synthesized,control,,control ") == ["synthesized", "control"]
    assert list(run.select_variants(tmp_path, ["synthesized", "control"])) == [
        "synthesized",
        "control",
    ]
    assert len(run.select_variants(tmp_path, [])) == 3
    with pytest.raises(SystemExit):
        run.select_variants(tmp_path, ["nope"])
    scenarios = [{"id": "a"}, {"id": "b"}]
    assert run.select_scenarios(scenarios, ["b"]) == [{"id": "b"}]


# ------------------------------------------------------------------ sign test and decision


def test_sign_test_p() -> None:
    assert score.sign_test_p(6, 0) == pytest.approx(0.03125)  # 2 * (1/2)^6
    assert score.sign_test_p(0, 6) == pytest.approx(0.03125)
    assert score.sign_test_p(5, 0) == pytest.approx(0.0625)
    assert score.sign_test_p(8, 2) == pytest.approx(0.109375)  # 2 * (1 + 10 + 45) / 1024
    assert score.sign_test_p(8, 1) == pytest.approx(0.0390625)  # 2 * (1 + 9) / 512
    assert score.sign_test_p(2, 1) == 1.0
    assert score.sign_test_p(0, 0) == 1.0


def _cell(variant: str, model: str, scenario: str, expect: bool, called: bool) -> dict[str, object]:
    return {
        "variant": variant,
        "model": model,
        "scenario": scenario,
        "repeat": 0,
        "class": "pos" if expect else "neg",
        "expect": expect,
        "recall_called": called,
        "first_tool": score.RECALL if called else "Grep",
        "n_phrasings": 3 if called else 0,
        "valid": True,
    }


def _grid(model: str, n_neg_won: int, pos_lost: int = 0) -> list[dict[str, object]]:
    """synthesized right and control wrong on n_neg_won negatives; 2 shared positives, of which
    synthesized misses ``pos_lost``."""
    rows = []
    for i in range(n_neg_won):
        rows += [
            _cell("control", model, f"n{i}", False, True),
            _cell("synthesized", model, f"n{i}", False, False),
        ]
    for i in range(2):
        rows += [
            _cell("control", model, f"p{i}", True, True),
            _cell("synthesized", model, f"p{i}", True, i >= pos_lost),
        ]
    return rows


def test_decision_rule(tmp_path: Path) -> None:
    ship = _grid("opus", 6) + _grid("sonnet", 6)
    verdict, lines = score.decide(ship)
    assert verdict == "VERDICT: SHIP synthesized" and len(lines) == 2
    report = score.build_report(ship, tmp_path / "r.jsonl")
    assert "## Decision" in report and "VERDICT: SHIP synthesized" in report
    assert "| opus | synthesized | 8 | 6 | 0 | 0.0312 | 2/2 vs 2/2 |" in report

    weak_sonnet = _grid("opus", 6) + _grid("sonnet", 2)  # 2-0, p = 0.5
    assert score.decide(weak_sonnet)[0] == "VERDICT: KEEP control"
    # 8 negatives won and one positive lost: 8-1, p = 0.039, but fewer positives elected.
    dropped = _grid("opus", 8, pos_lost=1) + _grid("sonnet", 8, pos_lost=1)
    assert score.paired(dropped, "synthesized", "control", "opus")["p"] < 0.05
    assert score.decide(dropped)[0] == "VERDICT: KEEP control"
    # An invalid synthesized cell drops its pair from the test.
    one_invalid = [dict(r, valid=False) if r["scenario"] == "n0" else r for r in ship]
    valid = [r for r in one_invalid if score.is_valid(r)]
    assert score.paired(valid, "synthesized", "control", "opus")["pairs"] == 7
    assert score.decide(valid)[0] == "VERDICT: KEEP control"  # 5-0 is p = 0.0625
    assert score.decide([])[0] == "VERDICT: KEEP control"


def test_invalid_reason_text(tmp_path: Path) -> None:
    # The 2026-09-10 rows: error was the stream's JSON tail, cut at 60 characters in the report.
    old = {
        **_cell("grep-contrast", "sonnet", "edit_task_2", False, False),
        "valid": False,
        "rc": 1,
        "result_subtype": "error_max_turns",
        "error": 'rc 1: ed":{"depth_limit":0,"concurrency_limit":0,"budget":0},"by_type":{}}',
    }
    new = {**old, "scenario": "x", "invalid_reason": "out_of_copy", "error": "out_of_copy: ..."}
    report = score.build_report([old, new, _cell("control", "sonnet", "x", True, True)], tmp_path)
    assert "- 1 x `error_max_turns`: grep-contrast/edit_task_2/sonnet/0" in report
    assert "- 1 x `out_of_copy`: grep-contrast/x/sonnet/0" in report
    assert "depth_limit" not in report
    assert score.invalid_reason({"error": "timeout after 150s", "rc": -1}) == "timeout"
    assert score.invalid_reason({"error": "rc 2: boom", "rc": 2}) == "rc 2"
