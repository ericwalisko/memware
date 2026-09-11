"""The plugin's foreground SessionStart entries: the notice that tells a plugin-only user what
`memware setup` has not asked them, and the digest that shows the model what memware holds for the
project. Everything else the plugin runs at session start is backgrounded with its output thrown
away, so these entries have to run in the foreground, and they have to stay harmless when the CLI
is missing or older than the plugin."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def _run(
    command: str, path: str, home: Path, payload: str = PAYLOAD, **env: str
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": path, "MEMWARE_HOME": str(home), **env}
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=payload,
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
    bindir = Path(sys.executable).parent
    if not (bindir / "memware").exists():
        pytest.skip("the memware script is not installed beside this interpreter")
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
    for path in (str(empty), f"{old}{os.pathsep}/bin{os.pathsep}/usr/bin"):
        out = _run(_hook(command)["command"], path, tmp_path)
        assert (out.returncode, out.stdout, out.stderr) == (0, "", "")
