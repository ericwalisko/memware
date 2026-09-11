"""``memware digest`` — the SessionStart block scoped to the project a session opens in.

The prompt hook injects beliefs only and nothing until the ledger fills, so this is the first
memware content most sessions see. These pin the Claude Code directory naming it scopes by, that
only the project's sessions and beliefs reach the block, that a worktree counts as its repository,
the cap, and that nothing it reads counts as a use. The hook entry that runs it is pinned beside
the notice's in test_plugin_hooks.py.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from memware.cli import main
from memware.digest import (
    FIRST_PROMPT_SQL,
    _base36,
    _js_string_hash,
    project_dir_name,
    sessions_sql,
)
from memware.ledger import Policy, assert_belief
from memware.store import Store
from tests.conftest import write_claude_jsonl

OPENING = "; call recall for past decisions, earlier sessions, anything not in the working tree."


@pytest.fixture()
def projects(tmp_path, monkeypatch) -> Path:
    """A Claude Code config dir of our own, so the developer's ~/.claude never enters."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    root = tmp_path / "claude" / "projects"
    root.mkdir(parents=True)
    return root


def _session(projects: Path, cwd: Path, session: str, day: str, prompt: str) -> None:
    folder = projects / project_dir_name(str(cwd))
    folder.mkdir(exist_ok=True)
    write_claude_jsonl(
        folder / f"{session}.jsonl",
        session,
        [
            ("assistant", f"2026-09-{day}T09:59:00Z", "an assistant line that is not the prompt"),
            ("user", f"2026-09-{day}T10:00:00Z", prompt),
            ("assistant", f"2026-09-{day}T10:01:00Z", "the reply to that prompt, long enough"),
        ],
    )


def _digest(db: str, capsys, *args: str, payload: dict | None = None, monkeypatch=None) -> str:
    capsys.readouterr()
    if payload is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        args = (*args, "--from-hook")
    assert main(["--db", db, "digest", *args]) == 0
    return capsys.readouterr().out


def _context(out: str) -> str:
    hook = json.loads(out)["hookSpecificOutput"]
    assert hook["hookEventName"] == "SessionStart"
    return str(hook["additionalContext"])


def test_directory_names_match_claude_code():
    """Claude Code 2.1: every non-alphanumeric UTF-16 unit becomes "-"; past 200 characters the
    name is cut and suffixed with base36(abs(the JavaScript string hash))."""
    assert project_dir_name("/Users/x/.hermes/hermes-agent") == "-Users-x--hermes-hermes-agent"
    assert project_dir_name("/a/b_c d") == "-a-b-c-d"
    assert project_dir_name("/a/\U0001f600") == "-a---"  # outside the BMP: two code units
    assert _js_string_hash("hello") == 99162322  # Java's "hello".hashCode()
    assert _js_string_hash("polygenelubricants") == -(2**31)
    assert _base36(2**31) == "zik0zk"
    long = "/" + "d" * 250
    name = project_dir_name(long)
    assert name == "-" + "d" * 199 + "-" + _base36(abs(_js_string_hash(long)))


def test_only_this_projects_sessions_newest_first(tmp_path, projects, capsys, monkeypatch):
    app, sibling = tmp_path / "work" / "app", tmp_path / "work" / "app-old"
    app.mkdir(parents=True)
    sibling.mkdir()
    _session(projects, app, "s1", "01", "wire the retry budget into the api client")
    _session(projects, app, "s2", "05", "move the port to 8443\nand update the docs")
    _session(projects, app, "now", "11", "this very session, just starting")
    _session(projects, sibling, "s3", "09", "a prompt from the other checkout")
    db = str(tmp_path / "m.db")
    main(["--db", db, "sync", str(projects)])

    ctx = _context(
        _digest(db, capsys, payload={"cwd": str(app), "session_id": "now"}, monkeypatch=monkeypatch)
    )
    lines = ctx.splitlines()
    assert lines[0] == "memware has 2 sessions and 0 beliefs for this project" + OPENING
    assert lines[1:] == [
        "Recent sessions here (last active, first prompt):",
        "- 2026-09-05 move the port to 8443 and update the docs",
        "- 2026-09-01 wire the retry budget into the api client",
    ]
    assert "other checkout" not in ctx  # "app-old" only begins like "app"
    assert "this very session" not in ctx  # the session starting is not news to itself

    plain = _digest(db, capsys, "--cwd", str(sibling))
    assert plain.startswith("memware has 1 session and 0 beliefs")
    assert "a prompt from the other checkout" in plain and "retry budget" not in plain


def test_nothing_for_an_unknown_project_or_a_missing_store(tmp_path, projects, capsys, monkeypatch):
    app, elsewhere = tmp_path / "app", tmp_path / "elsewhere"
    app.mkdir()
    elsewhere.mkdir()
    _session(projects, app, "s1", "01", "wire the retry budget into the api client")
    db = str(tmp_path / "m.db")
    main(["--db", db, "sync", str(projects)])
    with Store(db) as s:
        assert_belief(s, "elsewhere", "status", "has a belief but no session")

    assert _digest(db, capsys, "--cwd", str(elsewhere)) == ""
    assert _digest(db, capsys, payload={"cwd": str(elsewhere)}, monkeypatch=monkeypatch) == ""

    missing = tmp_path / "never.db"
    assert _digest(str(missing), capsys, "--cwd", str(app)) == ""
    assert not missing.exists()  # a hook must not create a store


def test_beliefs_whose_subject_names_the_directory_or_package(tmp_path, projects, capsys):
    app = tmp_path / "checkout"
    app.mkdir()
    (app / "pyproject.toml").write_text('[project]\nname = "widgetry"\n')
    (app / "package.json").write_text('{"name": "gadgetry"}')
    _session(projects, app, "s1", "01", "wire the retry budget into the api client")
    db = str(tmp_path / "m.db")
    main(["--db", db, "sync", str(projects)])
    with Store(db) as s:
        for subject, relation, value, when in [
            ("widgetry", "license", "MIT", "2026-08-01T00:00:00Z"),
            ("widgetry", "license", "Apache-2.0", "2026-09-02T00:00:00Z"),  # supersedes MIT
            ("checkout service", "listens on port", "8443", "2026-09-03T00:00:00Z"),
            ("gadgetry ui", "bundler", "vite", "2026-08-20T00:00:00Z"),
            ("billing", "depends on", "widgetry", "2026-09-04T00:00:00Z"),  # value, not subject
            ("checkouts", "count", "3", "2026-09-04T00:00:00Z"),  # a different whole term
        ]:
            assert_belief(s, subject, relation, value, valid_from=when, policy=Policy.AUTO)

    out = _digest(db, capsys, "--cwd", str(app))
    assert out.splitlines()[0].startswith("memware has 1 session and 3 beliefs for this project")
    assert out.endswith(
        "Current beliefs about this project:\n"
        "- checkout service listens on port: 8443 (since 2026-09-03)\n"
        "- widgetry license: Apache-2.0 (since 2026-09-02)\n"
        "- gadgetry ui bundler: vite (since 2026-08-20)\n"
    )
    assert "MIT" not in out and "billing" not in out and "checkouts" not in out


def test_a_worktree_counts_as_its_repository(tmp_path, projects, capsys):
    """Git's own files, no git process: a linked worktree's ``.git`` file points at the shared
    directory, whose ``worktrees/*/gitdir`` files name every live worktree."""
    primary, worktree, other = (
        tmp_path / "src" / "memware",
        tmp_path / "orca" / "memware" / "fix-port",
        tmp_path / "src" / "other",
    )
    admin = primary / ".git" / "worktrees" / "fix-port"
    admin.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (other / ".git").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {admin}\n")
    (admin / "gitdir").write_text(f"{worktree / '.git'}\n")
    (admin / "commondir").write_text("../..\n")
    _session(projects, primary, "p1", "01", "a session in the primary checkout")
    _session(projects, worktree, "w1", "02", "a session in the linked worktree")
    _session(projects, primary / "docs", "d1", "03", "a session started in a subdirectory")
    _session(projects, other, "o1", "04", "a session in another repository")
    db = str(tmp_path / "m.db")
    main(["--db", db, "sync", str(projects)])
    with Store(db) as s:
        assert_belief(s, "memware repo", "license", "MIT", valid_from="2026-09-02T00:00:00Z")

    for cwd in (worktree, primary):
        out = _digest(db, capsys, "--cwd", str(cwd))
        assert "memware has 2 sessions and 1 belief" in out, cwd
        assert "primary checkout" in out and "linked worktree" in out
        assert "another repository" not in out and "subdirectory" not in out
        assert "- memware repo license: MIT" in out  # the repository's name, from the worktree

    out = _digest(db, capsys, "--cwd", str(primary / "docs"))
    assert "memware has 3 sessions" in out and "subdirectory" in out


def test_a_submodule_is_its_own_project(tmp_path, projects, capsys):
    """A submodule's ``.git`` file holds a relative ``gitdir`` with no ``commondir``: nothing is
    shared, so the superproject's sessions stay out."""
    parent, sub = tmp_path / "super", tmp_path / "super" / "vendor" / "lib"
    (parent / ".git" / "modules" / "lib").mkdir(parents=True)
    sub.mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../../.git/modules/lib\n")
    _session(projects, parent, "p1", "01", "a session in the superproject")
    _session(projects, sub, "s1", "02", "a session in the submodule itself")
    db = str(tmp_path / "m.db")
    main(["--db", db, "sync", str(projects)])

    out = _digest(db, capsys, "--cwd", str(sub))
    assert "memware has 1 session" in out and "submodule itself" in out
    assert "superproject" not in out


def test_the_hooks_transcript_path_names_the_sessions_own_directory(
    tmp_path, projects, capsys, monkeypatch
):
    """Claude Code says where this session's transcript lives. Its directory counts even when
    the reported cwd no longer maps to it, e.g. after a ``cd`` and a compaction."""
    app, moved = tmp_path / "app", tmp_path / "moved"
    app.mkdir()
    moved.mkdir()
    _session(projects, app, "s1", "01", "wire the retry budget into the api client")
    _session(projects, app, "now", "11", "this very session, just starting")
    db = str(tmp_path / "m.db")
    main(["--db", db, "sync", str(projects)])

    transcript = projects / project_dir_name(str(app)) / "now.jsonl"
    payload = {"cwd": str(moved), "session_id": "now", "transcript_path": str(transcript)}
    ctx = _context(_digest(db, capsys, payload=payload, monkeypatch=monkeypatch))
    assert ctx.startswith("memware has 1 session and") and "retry budget" in ctx


def test_the_block_is_capped_and_every_prompt_is_one_clipped_line(tmp_path, projects, capsys):
    app = tmp_path / "app"
    app.mkdir()
    for i in range(8):
        _session(projects, app, f"s{i}", f"{i + 1:02d}", f"prompt {i} " + "word " * 80)
    db = str(tmp_path / "m.db")
    main(["--db", db, "sync", str(projects)])
    with Store(db) as s:
        for i in range(30):
            assert_belief(s, "app", f"fact {i}", "a value that takes up some room")

    def listed(out: str) -> tuple[list[str], list[str]]:
        lines = out.splitlines()
        return [ln for ln in lines if ln.startswith("- 2026-09-0")], [
            ln for ln in lines if ln.startswith("- app fact")
        ]

    out = _digest(db, capsys, "--cwd", str(app)).rstrip("\n")
    assert len(out) <= 1200
    assert out.splitlines()[0] == "memware has 8 sessions and 30 beliefs for this project" + OPENING
    sessions, beliefs = listed(out)
    assert [ln[:21] for ln in sessions] == [
        f"- 2026-09-0{d} prompt {d - 1}" for d in (8, 7, 6, 5, 4)
    ]
    assert all(len(ln) <= len("- 2026-09-01 ") + 120 for ln in sessions)
    assert 0 < len(beliefs) < 30  # what fits after five sessions, not all thirty

    out = _digest(db, capsys, "--cwd", str(app), "-k", "8").rstrip("\n")
    sessions, beliefs = listed(out)
    assert len(out) <= 1200 and 0 < len(sessions) < 8 and not beliefs  # the cap bites before k

    out = _digest(db, capsys, "--cwd", str(app), "-k", "2", "--max-chars", "500").rstrip("\n")
    assert len(out) <= 500 and len(listed(out)[0]) == 2

    out = _digest(db, capsys, "--cwd", str(app), "--max-chars", "10").rstrip("\n")
    assert out == "memware has 8 sessions and 30 beliefs for this project" + OPENING


def test_reading_the_digest_records_no_use(tmp_path, projects, capsys):
    app = tmp_path / "app"
    app.mkdir()
    _session(projects, app, "s1", "01", "wire the retry budget into the api client")
    db = str(tmp_path / "m.db")
    main(["--db", db, "sync", str(projects)])
    with Store(db) as s:
        assert_belief(s, "app", "listens on port", "8443")

    def uses() -> list[tuple]:
        with Store(db) as s:
            return [
                tuple(s.conn.execute(f"SELECT sum(use_count), max(last_used) FROM {t}").fetchone())
                for t in ("turn", "belief")
            ]

    before = uses()
    assert "8443" in _digest(db, capsys, "--cwd", str(app))
    assert uses() == before


def test_the_session_query_is_an_index_range_not_a_scan():
    with Store(":memory:") as s:
        sql = "EXPLAIN QUERY PLAN " + sessions_sql(3)
        plan = [r["detail"] for r in s.conn.execute(sql, ["/a/", "/a0"] * 3)]
        assert not any(d.startswith("SCAN") for d in plan), plan
        assert sum("USING INDEX sqlite_autoindex_turn_1" in d for d in plan) == 3, plan
        plan = [
            r["detail"] for r in s.conn.execute("EXPLAIN QUERY PLAN " + FIRST_PROMPT_SQL, ("s",))
        ]
        assert not any(d.startswith("SCAN") for d in plan), plan
