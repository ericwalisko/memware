"""File modes for what memware writes. The store holds transcripts, which hold whatever was pasted
into a session, so it is readable by its owner alone: files 0600, directories memware creates 0700.

SQLite creates a database 0644 less the umask, and its ``-wal`` and ``-shm`` files with the mode of
the database. So a store is created empty at 0600 before SQLite opens it, and a store an older
memware wrote is tightened when it next opens (:func:`tighten`). A directory memware did not create,
one ``MEMWARE_DB`` or ``backup.dest`` names, is left as it is: sharing it may be the point.

Every call is best-effort and POSIX-only: a mode memware cannot set (another user's file, a
filesystem without modes) never stops a store from opening. See docs/security.md."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from pathlib import Path

PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700

_POSIX = os.name == "posix"


def private_dir(path: str | os.PathLike[str]) -> Path:
    """``path`` and any missing parent, each created 0700. One that exists is left as it is."""
    target = Path(path)
    missing: list[Path] = []
    p = target
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        with contextlib.suppress(FileExistsError):
            d.mkdir(mode=PRIVATE_DIR)
    return target


def create_private(path: str | os.PathLike[str]) -> bool:
    """Create ``path`` empty at 0600 if nothing is there; True if this call created it. SQLite
    opens an empty file as an empty database, and it keeps the mode it finds."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def tighten(path: str | os.PathLike[str], mode: int = PRIVATE_FILE) -> bool:
    """Clear from ``path`` any permission bit ``mode`` does not grant, if the current user owns it.
    Returns True when it changed the mode. Never raises."""
    if sys.platform == "win32" or not _POSIX:
        return False
    try:
        st = os.stat(path)
        current = stat.S_IMODE(st.st_mode)
        if st.st_uid != os.geteuid() or not current & ~mode:
            return False
        os.chmod(path, current & mode)
        return True
    except OSError:
        return False


def tighten_store(db: str | os.PathLike[str]) -> None:
    """A store file and the SQLite files beside it: its write-ahead log and shared memory."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        tighten(f"{os.fspath(db)}{suffix}")
