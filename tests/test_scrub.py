"""An applied prune removes the text from the store file, not only from every query.

Pins #36. A SQLite ``DELETE`` frees pages without zeroing them unless ``secure_delete`` is on, and
builds disagree on that default. An FTS5 ``'delete'`` leaves the term, lowercased, in its segment
until a merge, which no case-sensitive search of the file sees. The write-ahead log keeps earlier
copies of pages while any other connection holds the store open. Every store here is synthetic,
under the test's own tmp dir.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memware import backup as bk
from memware.cli import main
from memware.ingest import prune, sync_tree
from memware.ledger import assert_belief
from memware.store import Store

VALUE = "HUNTER2SECRET"


@pytest.fixture()
def secure_delete_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every connection starts with ``secure_delete`` off, as Homebrew's SQLite starts it, whatever
    this build's default. A build compiled with SQLITE_SECURE_DELETE would otherwise zero freed
    pages on its own and pass a store that never asks for it."""
    real = sqlite3.connect

    def connect(*args, **kwargs):
        con = real(*args, **kwargs)
        con.execute("PRAGMA secure_delete=OFF")
        return con

    monkeypatch.setattr(sqlite3, "connect", connect)


def _corpus(tmp_path: Path) -> Path:
    """The issue's corpus: fifty ordinary lines, then one holding the value."""
    root = tmp_path / "corpus"
    root.mkdir()
    lines = [{"role": "user", "content": f"ordinary line {i} " + "z" * 200} for i in range(50)]
    lines.append({"role": "user", "content": f"the api token is {VALUE} and must not persist"})
    (root / "t.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return root


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "scratch.db"
    with Store(db) as s:
        sync_tree(s, _corpus(tmp_path), harness="generic")
    return db


def _counts(path: Path) -> tuple[int, int]:
    """The value in a file's bytes, as given and with ASCII case folded; (0, 0) with no file."""
    if not path.exists():
        return 0, 0
    data = path.read_bytes()
    return data.count(VALUE.encode()), data.lower().count(VALUE.lower().encode())


def _wal(db: Path) -> Path:
    return Path(f"{db}-wal")


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_an_applied_prune_leaves_no_copy_in_the_file_its_log_or_a_later_snapshot(
    tmp_path, capsys, secure_delete_off_by_default
):
    """#36: the issue's reproduction, on a connection that does not zero freed pages by default."""
    db = _db(tmp_path)
    assert _counts(db)[0] > 0

    code, _, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply")
    assert code == 0
    snapshot = bk.snapshot(db, tmp_path / "backups")

    assert _counts(db) == (0, 0)
    assert _counts(_wal(db)) == (0, 0)
    assert _counts(snapshot) == (0, 0)  # VACUUM INTO copied the FTS segments before the fix


def test_the_scrub_clears_the_index_even_with_secure_delete_off(tmp_path):
    """The pragma is not the fix on its own: an FTS5 tombstone keeps the lowercased term in a live
    page, which only a rebuild or a merge removes."""
    db = _db(tmp_path)
    with Store(db) as s:
        s.conn.execute("PRAGMA secure_delete=OFF")  # after the store turned it on
        assert prune(s, turns_containing=VALUE, apply=True).turns == 1
    assert _counts(db) == (0, 0)


def test_the_store_turns_secure_delete_on_whatever_the_build_default(
    tmp_path, secure_delete_off_by_default
):
    with Store(tmp_path / "s.db") as s:
        assert s.conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_the_log_is_emptied_while_another_connection_holds_the_store_open(tmp_path, capsys):
    """A running MCP server or Hermes provider keeps the -wal file from being deleted when the
    prune exits, and its frames hold the pages the sync wrote."""
    db = tmp_path / "scratch.db"
    with Store(db):
        pass
    holder = sqlite3.connect(db)
    holder.execute("SELECT count(*) FROM turn").fetchall()  # connected, and holding no snapshot
    try:
        with Store(db) as s:
            sync_tree(s, _corpus(tmp_path), harness="generic")
        assert _counts(_wal(db))[0] > 0

        code, _, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply")
        assert code == 0
        assert _counts(db) == (0, 0)
        assert _counts(_wal(db)) == (0, 0)
    finally:
        holder.close()


def test_a_reader_mid_read_keeps_the_log_and_the_prune_says_so(tmp_path, capsys):
    db = _db(tmp_path)
    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM turn").fetchall()
    try:
        with Store(db) as s:
            s.conn.execute("PRAGMA busy_timeout=50")  # the checkpoint's wait for the reader
            r = prune(s, turns_containing=VALUE, apply=True)
        assert r.turns == 1 and r.scrubbed is not None
        assert not r.scrubbed.wal_truncated

        from memware.cli import _scrub_notes, _scrubbed_line

        assert "NOT emptied" in _scrubbed_line(r.scrubbed)
        assert any("run the same prune again" in note for note in _scrub_notes(r, None))
    finally:
        reader.execute("COMMIT")
        reader.close()


def test_an_applied_prune_that_matches_nothing_still_scrubs_what_an_earlier_one_left(
    tmp_path, capsys
):
    """A store pruned before the fix: the turn is gone and its bytes are not. Running the prune
    again removes no turn, and is still how that store gets clean."""
    db = _db(tmp_path)
    with Store(db) as s:
        s.conn.execute("PRAGMA secure_delete=OFF")
        s.conn.execute("DELETE FROM turn WHERE instr(text, ?) > 0", (VALUE,))  # 0.6.1's prune
    assert _counts(db) != (0, 0)

    code, out, _ = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply", "--json"
    )
    assert code == 0 and json.loads(out)["turns_removed"] == 0
    assert _counts(db) == (0, 0)


def test_a_dry_run_scrubs_nothing(tmp_path, capsys):
    db = _db(tmp_path)
    with Store(db) as s:
        s.conn.execute("PRAGMA secure_delete=OFF")
        s.conn.execute("DELETE FROM turn WHERE instr(text, ?) > 0", (VALUE,))
    before = db.read_bytes()
    code, out, err = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE)
    assert code == 0 and "scrubbing" not in err and "store file" not in out
    assert db.read_bytes() == before


def test_the_dry_run_counts_the_beliefs_that_hold_the_text(tmp_path, capsys):
    """Prune keeps belief rows, retracted or not, so a belief naming the value keeps it in the
    file. The count is how a person learns that before applying."""
    db = _db(tmp_path)
    with Store(db) as s:
        assert_belief(s, "deploy", "api token", f"is {VALUE}")
        assert_belief(s, f"{VALUE} rotation", "is due", "on friday")
        assert_belief(s, "deploy", "region", "us-east-1")

    code, out, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--json")
    assert code == 0 and json.loads(out)["beliefs_holding_text"] == 2
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE)
    assert "beliefs holding the text : 2" in out

    code, out, err = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply")
    assert code == 0 and "turns removed : 1" in out
    assert "2 beliefs hold the text in a subject, relation or value" in err
    assert _counts(db)[0] > 0  # the belief rows still hold it: the decision in the PR


def test_a_glob_prune_counts_no_beliefs(tmp_path, capsys):
    db = _db(tmp_path)
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--glob", "*/t.jsonl", "--json")
    assert code == 0 and "beliefs_holding_text" not in json.loads(out)


def test_prune_apply_names_the_backup_destination_and_points_to_scan(tmp_path, capsys):
    db = _db(tmp_path)
    dest = tmp_path / "dropbox" / "memware"
    main(["--db", str(db), "config", "backup.dest", str(dest)])
    capsys.readouterr()

    code, out, err = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply")
    assert code == 0
    progress = [line for line in err.splitlines() if line.startswith("scrubbing the store file: ")]
    assert len(progress) == 3  # both indexes, then VACUUM, each announced before it runs
    assert "store file : scrubbed in " in out and "write-ahead log emptied" in out
    assert str(dest) in err and "`memware scan VALUE --backups`" in err
    assert VALUE not in out + err  # the value is never echoed back
    assert not dest.exists()  # a prune never writes to a backup destination

    code, out, _ = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply", "--json"
    )
    r = json.loads(out)
    assert r["backup_dest"] == str(dest)
    assert r["store_scrubbed"]["indexes"] == ["passage_fts", "belief_fts"]
    assert r["store_scrubbed"]["wal_truncated"] is True
