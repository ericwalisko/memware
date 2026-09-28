"""The plugin's foreground SessionStart entries: the notice that tells a plugin-only user what
`memware setup` has not asked them, and the digest that shows the model what memware holds for the
project. Everything else the plugin runs at session start is backgrounded with its output thrown
away, so these entries have to run in the foreground, and they have to stay harmless when the CLI
is missing or older than the plugin."""

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import pytest

from tests.conftest import write_claude_jsonl

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "integrations" / "claude-code" / "hooks" / "hooks.json"
PAYLOAD = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})


FOREGROUND = ["notice", "digest"]


def _hook(command: str) -> dict:
    groups = json.loads(HOOKS.read_text())["hooks"]["SessionStart"]
    entries = [h for g in groups for h in g["hooks"] if f"memware {command}" in h["command"]]
    assert len(entries) == 1, entries
    return entries[0]


@pytest.mark.parametrize("command", FOREGROUND)
def test_entry_runs_in_the_foreground(command):
    hook = _hook(command)
    assert hook["timeout"] == 5
    assert f"memware {command} --from-hook" in hook["command"]
    assert "nohup" not in hook["command"] and "&" not in hook["command"]
    assert ">/dev/null" not in hook["command"].replace("2>/dev/null", "")  # stdout reaches Claude


def _git_root() -> Path:
    """Git for Windows' install directory, found the way Claude Code finds its bash: from
    ``CLAUDE_CODE_GIT_BASH_PATH``, else from the ``git`` on PATH."""
    bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if bash:
        return Path(bash).parents[1]
    git = shutil.which("git")
    if git is not None:
        for d in Path(git).resolve().parents:
            if (d / "bin" / "bash.exe").exists():
                return d
    pytest.skip("Git for Windows is not installed")


def _shell() -> list[str]:
    """The shell Claude Code runs a hook command in: ``sh`` on POSIX, and on Windows the bash
    that Git for Windows installs."""
    if sys.platform == "win32":
        return [str(_git_root() / "bin" / "bash.exe"), "-c"]
    return ["/bin/sh", "-c"]


def _system_path() -> str:
    """PATH entries holding the shell's own tools and nothing of memware's."""
    if sys.platform == "win32":
        return str(_git_root() / "usr" / "bin")
    return f"/bin{os.pathsep}/usr/bin"


def _scripts() -> Path:
    """Where pip put the ``memware`` console script for this interpreter."""
    bindir = Path(sys.executable).parent
    if sys.platform == "win32":
        bindir = Path(sysconfig.get_path("scripts"))
    if shutil.which("memware", path=str(bindir)) is None:
        pytest.skip("the memware script is not installed beside this interpreter")
    return bindir


def _run(
    command: str, path: str, home: Path, payload: str = PAYLOAD, **env: str
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": path, "MEMWARE_HOME": str(home), **env}
    return subprocess.run(
        [*_shell(), command],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_notice_hook_command_prints_hook_json(tmp_path):
    """The command string exactly as Claude Code runs it, through a shell."""
    bindir = _scripts()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"setup": {"completed_version": "0.2.5"}}))

    out = _run(_hook("notice")["command"], f"{bindir}{os.pathsep}{os.environ['PATH']}", home)
    assert out.returncode == 0, out.stderr
    assert "memware setup" in json.loads(out.stdout)["systemMessage"]


@pytest.mark.parametrize(
    ("event", "command"),
    [
        ("SessionStart", "memware notice"),
        ("SessionStart", "memware digest"),
        ("UserPromptSubmit", "memware context"),
        ("PreCompact", "memware sync"),
    ],
)
def test_foreground_hooks_list_a_no_capture_session(tmp_path, event, command):
    """Under MEMWARE_NO_CAPTURE every foreground entry puts the session's transcript on the
    no-capture list, through the command string exactly as Claude Code runs it. The start
    entries cover a session that is later force-killed; the backgrounded catch-up and backup
    never see the variable and learn it from the list."""
    bindir = _scripts()
    groups = json.loads(HOOKS.read_text())["hooks"][event]
    entries = [h for g in groups for h in g["hooks"] if f"{command} " in h["command"]]
    assert len(entries) == 1, entries
    home = tmp_path / "home"
    transcript = tmp_path / "projects" / "p" / "s.jsonl"  # Claude Code writes it later
    payload = json.dumps(
        {
            "session_id": "s",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "hook_event_name": event,
            "source": "startup",
            "prompt": "hi",
        }
    )

    out = _run(
        entries[0]["command"],
        f"{bindir}{os.pathsep}{os.environ['PATH']}",
        home,
        payload,
        MEMWARE_NO_CAPTURE="1",
    )

    assert out.returncode == 0, out.stderr
    assert (home / "no-capture.txt").read_text() == f"{transcript.resolve()}\n"


@pytest.mark.parametrize("command", FOREGROUND)
def test_hook_command_is_harmless_without_the_cli(tmp_path, command):
    """No memware on PATH, or one too old to know the command: exit 0 and print nothing, rather
    than put a shell or usage error in front of the user at every session start."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    old = tmp_path / "old-bin"
    old.mkdir()
    (old / "memware").write_text("#!/bin/sh\necho 'memware: error: invalid choice' >&2\nexit 2\n")
    (old / "memware").chmod(0o755)
    for path in (str(empty), f"{old}{os.pathsep}{_system_path()}"):
        out = _run(_hook(command)["command"], path, tmp_path)
        assert (out.returncode, out.stdout, out.stderr) == (0, "", "")


NODE_HOOK = """
const { spawn } = require("child_process");
const [shell, flag, command, payload] = process.argv.slice(2);
const child = spawn(shell, [flag, command], { stdio: ["pipe", "pipe", "pipe"] });
child.stdin.on("error", () => {});  // a hook that backgrounds everything never reads it
child.stdin.end(payload);
child.on("close", (code) => { process.stdout.write(String(code)); process.exit(0); });
"""


def _turns(db: Path) -> int:
    if not db.exists():
        return 0
    try:
        with contextlib.closing(sqlite3.connect(db, timeout=1)) as conn:
            return int(conn.execute("SELECT count(*) FROM turn").fetchone()[0])
    except sqlite3.Error:
        return 0


def test_a_backgrounded_sync_outlives_the_process_that_ran_the_hook(tmp_path):
    """Claude Code is a Node process and may exit as soon as a hook returns. The SessionStart
    catch-up sync is backgrounded so the hook returns within its timeout, and it must still
    finish once Node has gone: on Windows, outlive the job object Node spawns its children in.

    A backgrounded command reads its stdin from /dev/null, so it never sees the hook's payload:
    the catch-up syncs the configured transcript source, not the one session."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    bindir = _scripts()
    [entry] = [
        h for g in json.loads(HOOKS.read_text())["hooks"]["SessionStart"] for h in g["hooks"]
    ][:1]
    assert "nohup" in entry["command"] and "memware sync --harness claude-code;" in entry["command"]
    projects = tmp_path / "projects"
    transcript = projects / "p" / "s.jsonl"
    transcript.parent.mkdir(parents=True)
    write_claude_jsonl(
        transcript,
        "s",
        [
            ("user", "2026-09-11T00:00:00Z", "where does the build cache live"),
            ("assistant", "2026-09-11T00:00:05Z", "on the NAS, under /cache"),
        ],
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"backup": {"transcript_src": str(projects)}}))
    db = tmp_path / "store.db"
    runner = tmp_path / "hook.js"
    runner.write_text(NODE_HOOK)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "MEMWARE_HOME": str(home),
        "MEMWARE_DB": str(db),
    }

    start = time.monotonic()
    out = subprocess.run(
        [node, str(runner), *_shell(), entry["command"], PAYLOAD],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert out.stdout == "0", out.stderr
    assert time.monotonic() - start < entry["timeout"]

    deadline = time.monotonic() + 60
    while _turns(db) < 2 and time.monotonic() < deadline:
        time.sleep(0.25)
    assert _turns(db) == 2
