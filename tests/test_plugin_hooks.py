"""The plugin's SessionStart entry that tells a plugin-only user what `memware setup` has not asked
them. Everything else the plugin runs at session start is backgrounded with its output thrown
away, so this entry has to run in the foreground, and it has to stay harmless when the CLI is
missing or older than the plugin."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "integrations" / "claude-code" / "hooks" / "hooks.json"
PAYLOAD = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})


def _notice_hook() -> dict:
    groups = json.loads(HOOKS.read_text())["hooks"]["SessionStart"]
    entries = [h for g in groups for h in g["hooks"] if "memware notice" in h["command"]]
    assert len(entries) == 1, entries
    return entries[0]


def test_notice_entry_runs_in_the_foreground():
    hook = _notice_hook()
    assert hook["timeout"] == 5
    assert "memware notice --from-hook" in hook["command"]
    assert "nohup" not in hook["command"] and "&" not in hook["command"]
    assert ">/dev/null" not in hook["command"].replace("2>/dev/null", "")  # stdout reaches Claude


def _run(command: str, path: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": path, "MEMWARE_HOME": str(home)}
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=PAYLOAD,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_notice_hook_command_prints_hook_json(tmp_path):
    """The command string exactly as Claude Code runs it, through a shell."""
    bindir = Path(sys.executable).parent
    if not (bindir / "memware").exists():
        pytest.skip("the memware script is not installed beside this interpreter")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"setup": {"completed_version": "0.2.5"}}))

    out = _run(_notice_hook()["command"], f"{bindir}{os.pathsep}{os.environ['PATH']}", home)
    assert out.returncode == 0, out.stderr
    assert "memware setup" in json.loads(out.stdout)["systemMessage"]


def test_notice_hook_command_is_harmless_without_the_cli(tmp_path):
    """No memware on PATH, or one too old to know `notice`: exit 0 and print nothing, rather than
    put a shell or usage error in front of the user at every session start."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    old = tmp_path / "old-bin"
    old.mkdir()
    (old / "memware").write_text("#!/bin/sh\necho 'memware: error: invalid choice' >&2\nexit 2\n")
    (old / "memware").chmod(0o755)
    for path in (str(empty), f"{old}{os.pathsep}/bin{os.pathsep}/usr/bin"):
        out = _run(_notice_hook()["command"], path, tmp_path)
        assert (out.returncode, out.stdout, out.stderr) == (0, "", "")
