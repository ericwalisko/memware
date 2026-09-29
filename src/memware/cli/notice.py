"""`memware notice`, and the setup hint other commands print."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from memware import __version__
from memware.cli._common import AddCommand, _count, _hook_payload, _hook_store, _out, _stale
from memware.derive import open_readonly
from memware.digest import injection_gate, resolve_project
from memware.store import now_iso
from memware.volatile import older_version

# Features that send data somewhere new, by the version that added them. A setup run on an
# older version never asked about them, so the hint re-asks until setup runs on that version or
# later, or the feature's `<name>.auto` switch has been written either way.
CONSENT: dict[str, str] = {"0.4.0": "derive"}


_CONSENT_HINTS = {
    "derive": "memware {version} added `derive`, which sends transcript excerpts to a model. "
    "Run `memware setup` to enable or decline; `memware derive --plan` previews with no "
    "network call.",
}


def _older(version: str, than: str) -> bool:
    """Whether ``version`` is older than ``than``, compared as versions, never as strings
    (as strings, "0.10.0" sorts before "0.4.0"). A version that cannot be read is older: setup
    never recorded one it could read, so it never asked."""
    return older_version(version, than) is not False


def _consent_hints(done: object) -> list[str]:
    """The hint for each CONSENT feature setup has not asked about — setup never ran (``done`` is
    empty), or last ran on an older version — and whose switch was never written."""
    from memware.config import has_key

    return [
        _CONSENT_HINTS[feature].format(version=version)
        for version, feature in CONSENT.items()
        if (not done or _older(str(done), version)) and not has_key(f"{feature}.auto")
    ]


def _maybe_setup_hint(a: argparse.Namespace) -> None:
    """One-line nudges to `memware setup`, on stderr; silent from hooks and in --json mode.

    Backups: for anyone who has never configured them — new installs and upgrades from a
    pre-backup (pre-0.2) version alike; stops once setup has run or a destination is set.
    Consent: see ``_consent_hints``; `memware notice` carries the same lines to plugin users."""
    from memware.config import get_dotted, load_config

    if getattr(a, "from_hook", False) or getattr(a, "json", False):
        return
    cfg = load_config()
    done = get_dotted(cfg, "setup.completed_version")
    if not done and not get_dotted(cfg, "backup.dest"):
        print(
            "Tip: run `memware setup` to configure backups (one time; this hint then stops).",
            file=sys.stderr,
        )
    for hint in _consent_hints(done):
        print(hint, file=sys.stderr)


STALE_NOTICE = "stale-beliefs"
"""The key in the store's ``notice`` table that records the stale-belief notice was given."""


def _notice_given(db: str, key: str) -> bool:
    """Whether the store records notice ``key``, read through a handle that takes no lock. A
    store older than the ``notice`` table has not given it."""
    try:
        conn = open_readonly(db)
    except (OSError, sqlite3.Error):
        return False
    try:
        return conn.execute("SELECT 1 FROM notice WHERE key=?", (key,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _stale_notice(a: argparse.Namespace, cwd: object) -> list[str]:
    """Once per store: how many beliefs injection now leaves out, and the command that lists
    them. Every session start after the first is one read that takes no lock. The first counts,
    and records in the store's ``notice`` table that it ran, whether or not it had anything to
    say; that write waits at most a quarter second for another writer and is skipped rather than
    stall the session start (the next one tries again). A marker in the store, not the memware
    home, so a home that will not parse or take a write changes nothing. A store that is not
    there is never created."""
    if a.db == ":memory:" or not Path(a.db).expanduser().exists():
        return []  # no store yet: nothing was ever injected, and a hook must not create one
    if _notice_given(a.db, STALE_NOTICE):
        return []
    store = _hook_store(a.db)
    if store is None:
        return []  # locked mid-upgrade: the next session start says it
    with store as s:
        n = len(_stale(s, injection_gate(resolve_project(Path(str(cwd or os.getcwd()))))))
        try:
            claimed = s.conn.execute(
                "INSERT OR IGNORE INTO notice(key, shown_at, version) VALUES (?,?,?)",
                (STALE_NOTICE, now_iso(), __version__),
            ).rowcount
        except sqlite3.OperationalError:  # locked: say it now, record it next time
            claimed = 1
    if not n or not claimed:
        return []  # nothing to say, or a session starting alongside said it
    return [
        f"memware no longer injects {_count(n, 'belief')} that "
        f"{'was true when recorded and needs' if n == 1 else 'were true when recorded and need'} "
        "re-checking now (a measurement, a moving version or a status, or a version the project "
        "manifest overrules). `memware beliefs --stale` lists "
        f"{'it' if n == 1 else 'them'} and why; `memware beliefs retract --stale --apply` "
        f"retracts {'it' if n == 1 else 'them'}."
    ]


def cmd_notice(a: argparse.Namespace) -> int:
    """What the person should hear at session start, for someone who only uses the plugin: they
    never type a memware command, so they never see what `stats` prints. The plugin runs this in
    the foreground at session start, and Claude Code shows the ``systemMessage`` to them. The
    consent hints read the config file. The stale-belief notice reads the store's one-time marker
    without a lock, counts only the first time, and never creates a store. Whatever goes wrong, it prints nothing and exits 0: it cannot fail a session
    start."""
    try:
        from memware.config import config_path, get_dotted

        payload = _hook_payload() if a.from_hook else {}
        if payload.get("source") == "compact":
            return 0  # a compaction mid-session, not a session the person just opened
        # No file means nobody has answered. A file that will not parse is not an answer either,
        # but it may hold one: stay quiet rather than ask at every session start.
        path = config_path()
        user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(user, dict):
            return 0
        hints = _consent_hints(get_dotted(user, "setup.completed_version"))
        with contextlib.suppress(Exception):
            hints += _stale_notice(a, payload.get("cwd"))
        if a.from_hook:
            if hints:
                print(json.dumps({"systemMessage": "\n".join(hints)}))
        else:
            _out(hints, a.json)
    except Exception:
        pass
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "notice",
        "print what `memware setup` has not asked about yet (the plugin shows it at session start)",
        epilog=(
            "Examples:\n"
            "  memware notice               one line per pending question; nothing when none\n"
            "  memware notice --from-hook   the same as Claude Code hook JSON (systemMessage)"
        ),
    )
    s.add_argument(
        "--from-hook",
        action="store_true",
        help="print hook JSON with a systemMessage, and nothing after a compaction",
    )
    s.set_defaults(fn=cmd_notice)
