"""What memware does the same way on every OS it supports: the lock the no-capture list is written
under, the probe for a live process that the derive lock trusts, and the name Claude Code gives a
project's transcript directory when the project lives on a Windows drive."""

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from memware.derive import _pid_alive
from memware.digest import project_dir_name
from memware.ingest import _exclusive, no_capture_file

HOLD = """
import sys
from pathlib import Path
from memware.ingest import _exclusive
with _exclusive(Path(sys.argv[1])):
    print("held", flush=True)
    sys.stdin.read()
"""


def _holder(lock: Path) -> subprocess.Popen[str]:
    """Another process holding ``lock`` until its stdin closes."""
    p = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert p.stdout is not None and p.stdout.readline().strip() == "held"
    return p


def _taker(lock: Path) -> tuple[threading.Thread, threading.Event]:
    got = threading.Event()

    def take() -> None:
        with _exclusive(lock):
            got.set()

    t = threading.Thread(target=take, daemon=True)
    t.start()
    return t, got


def _finish(p: subprocess.Popen[str]) -> None:
    for stream in (p.stdin, p.stdout):
        if stream is not None and not stream.closed:
            stream.close()
    p.wait(timeout=30)


def test_the_lock_excludes_another_process_until_it_lets_go(tmp_path):
    lock = tmp_path / "list.lock"
    holder = _holder(lock)
    try:
        t, got = _taker(lock)
        assert not got.wait(0.5), "took a lock another process holds"
    finally:
        _finish(holder)
    assert got.wait(15), "never took the lock after its holder let go"
    t.join(15)


def test_a_killed_holder_does_not_leave_the_lock_held(tmp_path):
    """The OS drops the lock with the process, so a hook killed mid-write blocks nobody."""
    lock = tmp_path / "list.lock"
    holder = _holder(lock)
    holder.kill()
    _finish(holder)
    t, got = _taker(lock)
    assert got.wait(15)
    t.join(15)


def test_the_lock_excludes_another_thread(tmp_path):
    lock = tmp_path / "list.lock"
    with _exclusive(lock):
        t, got = _taker(lock)
        assert not got.wait(0.3)
    assert got.wait(15)
    t.join(15)


RECORD = """
import sys
from memware.ingest import record_no_capture
for p in sys.argv[1:]:
    record_no_capture(p)
"""


def test_processes_that_record_at_once_all_land(tmp_path):
    """Sessions an eval harness starts together each run their own hook process. Without a lock
    that works across processes, two that read the list together drop one another's path."""
    paths = [tmp_path / f"s{i}.jsonl" for i in range(96)]
    procs = [
        subprocess.Popen([sys.executable, "-c", RECORD, *map(str, paths[i::6])], env=os.environ)
        for i in range(6)
    ]
    assert [p.wait(timeout=120) for p in procs] == [0] * 6
    lines = no_capture_file().read_text(encoding="utf-8").splitlines()
    assert sorted(lines) == sorted(str(p.resolve()) for p in paths)


def test_probing_a_live_process_leaves_it_running():
    """The derive lock asks whether its holder is alive. On Windows ``os.kill(pid, 0)`` is not a
    probe: it terminates the process."""
    p = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        assert _pid_alive(p.pid)
        with pytest.raises(subprocess.TimeoutExpired):
            p.wait(timeout=0.5)
        assert _pid_alive(os.getpid())
    finally:
        assert p.stdin is not None
        p.stdin.close()
        p.wait(timeout=30)
    assert not _pid_alive(p.pid)
    assert not _pid_alive(999_999_999)


@pytest.mark.parametrize(
    ("cwd", "name"),
    [
        (r"C:\Users\eric\dev\memware", "C--Users-eric-dev-memware"),
        (r"D:\a\memware\memware", "D--a-memware-memware"),
        (
            r"C:\Users\eric\dev\my.app\.claude\worktrees\feat",
            "C--Users-eric-dev-my-app--claude-worktrees-feat",
        ),
        (r"\\server\share\proj", "--server-share-proj"),
    ],
)
def test_a_windows_project_directory_is_named_as_claude_code_names_it(cwd, name):
    """Claude Code makes every character of the cwd but a letter or digit a dash, the drive's
    colon and each backslash included."""
    assert project_dir_name(cwd) == name
