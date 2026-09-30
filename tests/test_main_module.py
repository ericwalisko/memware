"""``python -m memware`` runs the same entry point as the ``memware`` script (card t_60a4a07d).

Each case runs in a subprocess, because the failure being pinned is the interpreter's own:
``No module named memware.__main__``."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from memware import __version__

# What the ``memware = memware.cli:main`` console script does, without needing it on PATH.
_SCRIPT = [sys.executable, "-c", "import sys; from memware.cli import main; sys.exit(main())"]
_MODULE = [sys.executable, "-m", "memware"]


def _run(cmd: list[str], *args: str, tmp_path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "COLUMNS": "80",  # argparse wraps --help to the terminal width
        "MEMWARE_HOME": str(tmp_path),  # the --db default in --help names the home
        "MEMWARE_DB": "",
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run([*cmd, *args], capture_output=True, text=True, env=env, timeout=60)


def test_python_dash_m_memware_reports_the_version(tmp_path):
    r = _run(_MODULE, "--version", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == __version__


def test_python_dash_m_memware_help_equals_the_script_help(tmp_path):
    via_module = _run(_MODULE, "--help", tmp_path=tmp_path)
    via_script = _run(_SCRIPT, "--help", tmp_path=tmp_path)
    assert via_module.returncode == 0, via_module.stderr
    assert via_module.stdout.startswith("usage: memware ")
    assert via_module.stdout == via_script.stdout


def test_python_dash_m_memware_passes_a_subcommand_and_its_exit_code(tmp_path):
    db = str(tmp_path / "m.db")
    ok = _run(_MODULE, "--db", db, "stats", "--json", tmp_path=tmp_path)
    assert ok.returncode == 0, ok.stderr
    assert '"beliefs_current"' in ok.stdout
    bad = _run(_MODULE, "no-such-command", tmp_path=tmp_path)
    assert bad.returncode == 2
    assert "invalid choice" in bad.stderr


@pytest.mark.parametrize("module", ["memware", "memware.cli"])
def test_both_module_spellings_agree(module, tmp_path):
    r = _run([sys.executable, "-m", module], "--version", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == __version__
