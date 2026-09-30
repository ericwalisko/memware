"""Behaviour of the smaller commands, one module each under ``memware.cli``: what they print,
what they exit with, and what they leave on disk. Fixtures are synthetic."""

from __future__ import annotations

import io
import json
import runpy
import sys

import pytest

from memware import backup as bk
from memware.cli import main
from memware.cli.nuke import NUKE_PHRASE
from memware.store import Store

TURN = (
    "INSERT INTO turn(session,seq,ts,role,text,source,harness) "
    "VALUES ('sess',?,'2026-08-01T00:00:00Z','assistant',?,'x','claude-code')"
)


def _store_with_turns(db, n):
    with Store(db) as s:
        for i in range(n):
            s.conn.execute(TURN, (i, f"turn {i}: the cache layer keeps a retry policy"))
    return db


def _turns(db):
    with Store(db) as s:
        return s.stats()["turns"]


# ---- init -------------------------------------------------------------------------------


def test_init_creates_the_store_and_reports_it_as_json(tmp_path, capsys):
    db = tmp_path / "sub" / "m.db"
    assert main(["--db", str(db), "--json", "init"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["db"] == str(db) and out["turns"] == 0
    assert db.exists()


# ---- read -------------------------------------------------------------------------------


def test_read_lists_a_session_and_a_window_around_a_turn(tmp_path, capsys):
    db = _store_with_turns(tmp_path / "m.db", 7)
    assert main(["--db", str(db), "--json", "read", "sess"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["seq"] for r in rows] == list(range(7))

    anchor = rows[3]["id"]
    assert main(["--db", str(db), "--json", "read", "sess", "--around", str(anchor)]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 7  # the default window covers all of it
    assert (
        main(["--db", str(db), "--json", "read", "sess", "--around", str(anchor), "--window", "1"])
        == 0
    )
    assert [r["seq"] for r in json.loads(capsys.readouterr().out)] == [2, 3, 4]

    assert (
        main(["--db", str(db), "--plain", "read", "sess", "--around", str(anchor), "--window", "0"])
        == 0
    )
    (line,) = capsys.readouterr().out.splitlines()
    assert line.split("\t")[:3] == [str(anchor), "3", "assistant"]

    assert main(["--db", str(db), "--json", "read", "no-such-session"]) == 0
    assert json.loads(capsys.readouterr().out) == []


# ---- restore ----------------------------------------------------------------------------


def test_restore_from_a_named_snapshot_saves_the_current_store_aside(tmp_path, capsys):
    db = _store_with_turns(tmp_path / "m.db", 5)
    snap = bk.snapshot(db, tmp_path / "bk")
    with Store(db) as s:
        s.conn.execute("DELETE FROM turn")
    assert main(["--db", str(db), "--json", "restore", "--from", str(snap)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["restored_from"] == str(snap) and out["turns"] == 5
    assert _turns(db) == 5
    assert _turns(out["previous_store_saved_to"]) == 0  # the wiped store is kept, not lost


def test_restore_without_a_file_takes_the_newest_snapshot_in_the_destination(tmp_path, capsys):
    db = _store_with_turns(tmp_path / "m.db", 2)
    dest = tmp_path / "bk"
    old = bk.snapshot(db, dest)
    old.rename(dest / "memware-20200101-000000.db")  # snapshot names carry their time
    with Store(db) as s:
        s.conn.execute(TURN, (9, "a later turn that only the newer snapshot holds"))
    new = bk.snapshot(db, dest)
    with Store(db) as s:
        s.conn.execute("DELETE FROM turn")

    assert main(["--db", str(db), "--json", "restore", "--dest", str(dest)]) == 0
    assert json.loads(capsys.readouterr().out)["restored_from"] == str(new)
    assert _turns(db) == 3


def test_restore_finds_the_destination_in_the_config(tmp_path, capsys):
    db = _store_with_turns(tmp_path / "m.db", 1)
    dest = tmp_path / "configured"
    snap = bk.snapshot(db, dest)
    assert main(["--db", str(db), "config", "backup.dest", str(dest)]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "--json", "restore"]) == 0
    assert json.loads(capsys.readouterr().out)["restored_from"] == str(snap)


def test_restore_says_what_is_missing_and_exits_2(tmp_path, capsys):
    db = tmp_path / "m.db"
    assert main(["--db", str(db), "restore"]) == 2
    assert "no backup destination configured; pass --from FILE" in capsys.readouterr().err

    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["--db", str(db), "restore", "--dest", str(empty)]) == 2
    assert f"no snapshots in {empty}" in capsys.readouterr().err
    assert not db.exists()  # nothing was replaced or created


# ---- nuke -------------------------------------------------------------------------------


def test_nuke_prompts_and_a_closed_stdin_deletes_nothing(tmp_path, capsys, monkeypatch):
    db = _store_with_turns(tmp_path / "m.db", 1)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # EOF at the prompt
    assert main(["--db", str(db), "nuke"]) == 1
    captured = capsys.readouterr()
    assert f"Type exactly:  {NUKE_PHRASE}" in captured.out
    assert "nothing deleted" in captured.err
    assert db.exists()


def test_nuke_takes_the_typed_phrase_at_the_prompt(tmp_path, capsys, monkeypatch):
    db = _store_with_turns(tmp_path / "m.db", 1)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"  {NUKE_PHRASE}  \n"))
    assert main(["--db", str(db), "--json", "nuke"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{") :]) == {"deleted_files": 1, "snapshots_deleted": 0}
    assert not db.exists()


# ---- completions ------------------------------------------------------------------------


def test_completions_name_every_command(capsys):
    pytest.importorskip("shtab")
    assert main(["completions", "bash"]) == 0
    script = capsys.readouterr().out
    for command in ("recall", "beliefs", "prune", "completions"):
        assert command in script


def test_completions_without_shtab_explain_the_extra(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "shtab", None)  # the import now fails
    assert main(["completions", "zsh"]) == 2
    assert "pip install 'memware[shell]'" in capsys.readouterr().err


# ---- python -m memware.cli ---------------------------------------------------------------


def test_python_dash_m_runs_the_same_entry_point(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["memware", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("memware.cli", run_name="__main__")
    assert exc.value.code == 0
    from memware import __version__

    assert capsys.readouterr().out.strip() == __version__
