"""Transcripts are sensitive, so everything memware writes that holds their text is private to its
owner: files 0600, directories memware creates 0700. A store written by an older memware under the
default umask (0644 in a 0755 home) is tightened the next time it opens. See docs/security.md."""

import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from memware import backup
from memware.config import save_config
from memware.store import Store

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


@pytest.fixture(autouse=True)
def permissive_umask() -> Iterator[None]:
    """The umask most machines ship with, under which files come out 0644 unless memware says
    otherwise."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _home() -> Path:
    return Path(os.environ["MEMWARE_HOME"])


def test_a_new_store_and_its_home_are_private():
    db = _home() / "memware.db"
    with Store(db) as s:
        s.conn.execute(
            "INSERT INTO cursor(source, offset, seq, updated_at) VALUES ('x', 0, 0, 'y')"
        )
        assert _mode(db) == 0o600
        assert _mode(Path(f"{db}-wal")) == 0o600
    assert _mode(_home()) == 0o700


def test_an_existing_store_is_tightened_on_open():
    """A store an older memware wrote under the default umask: the file, its write-ahead log and
    shared-memory file 0644, the home 0755."""
    home = _home()
    home.mkdir(mode=0o755)
    db = home / "memware.db"
    holder = sqlite3.connect(str(db))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE t(x)")
    holder.execute("INSERT INTO t VALUES (1)")
    holder.commit()  # the log stays while this connection is open
    try:
        for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
            assert _mode(p) == 0o644, p
        with Store(db):
            pass
        for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
            assert _mode(p) == 0o600, p
        assert _mode(home) == 0o700
    finally:
        holder.close()


def test_a_store_outside_the_home_leaves_its_directory_alone(tmp_path: Path):
    """``MEMWARE_DB`` may point into a directory the user shares on purpose: the file is private,
    the directory is not memware's to change."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    db = shared / "memware.db"
    with Store(db):
        pass
    assert _mode(db) == 0o600
    assert _mode(shared) == 0o755


def test_a_store_in_a_new_directory_creates_it_private(tmp_path: Path):
    db = tmp_path / "a" / "b" / "memware.db"
    with Store(db):
        pass
    assert _mode(db.parent) == 0o700
    assert _mode(db.parent.parent) == 0o700


def test_snapshots_are_private(tmp_path: Path):
    db = tmp_path / "t.db"
    with Store(db):
        pass
    dest = tmp_path / "backups"
    snap = backup.snapshot(db, dest)
    assert _mode(snap) == 0o600
    assert _mode(dest) == 0o700


def test_a_restored_store_is_private(tmp_path: Path):
    """A snapshot an older memware wrote is 0644; the store restored from it must not be."""
    db = tmp_path / "t.db"
    with Store(db):
        pass
    old = tmp_path / "memware-20260101-000000.db"
    con = sqlite3.connect(str(old))
    con.execute("CREATE TABLE t(x)")
    con.close()
    assert _mode(old) == 0o644
    safety = backup.restore(old, db)
    assert _mode(db) == 0o600
    assert _mode(safety) == 0o600


def test_the_home_config_writes_create_is_private():
    save_config({"backup": {"dest": None}})
    assert _mode(_home()) == 0o700
