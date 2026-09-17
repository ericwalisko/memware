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

import memware.store as store_module
from memware import backup as bk
from memware.cli import main
from memware.derive import source_pointer
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


def _derived_belief(db: Path) -> None:
    """A belief derive filed from the turn holding the value, so a prune that removes the turn
    empties its session and retracts the belief."""
    with Store(db) as s:
        turn = s.conn.execute(
            "SELECT id, session FROM turn WHERE instr(text, ?) > 0", (VALUE,)
        ).fetchone()
        s.conn.execute("DELETE FROM turn WHERE session=? AND id<>?", (turn["session"], turn["id"]))
        assert_belief(
            s,
            "api",
            "uses token of kind",
            "bearer",
            source=source_pointer(turn["session"], turn["id"]),
        )


def test_a_prune_never_writes_its_text_into_a_retraction_reason(tmp_path, capsys):
    """Review of #40: the retraction's reason recorded the command line, text included, so the
    prune wrote the value back into the file it had just scrubbed."""
    db = _db(tmp_path)
    _derived_belief(db)

    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply", "--json"
    )
    r = json.loads(out)
    assert code == 0 and r["turns_removed"] == 1 and len(r["retract"]) == 1
    assert _counts(db) == (0, 0) and _counts(_wal(db)) == (0, 0)
    with Store(db) as s:
        (reason,) = s.conn.execute("SELECT reason FROM retraction").fetchone()
    assert reason.endswith("(memware prune --turns-containing (value withheld))")
    assert r["left_in_store"]["leftover"] is False


def test_a_reason_an_older_prune_wrote_is_rewritten(tmp_path, capsys):
    """A store pruned by 0.6.0 or 0.6.1 holds the text in a retraction reason and nowhere else.
    Running the prune again rewrites the reason and scrubs the file."""
    db = _db(tmp_path)
    _derived_belief(db)
    with Store(db) as s:
        prune(s, turns_containing=VALUE, apply=True)
        s.conn.execute(  # what 0.6.1 recorded
            "UPDATE retraction SET reason = ?",
            (f"session s is no longer indexed (memware prune --turns-containing {VALUE!r})",),
        )
    assert _counts(db)[0] == 1  # the old command line, in the reason

    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply", "--json"
    )
    r = json.loads(out)
    assert code == 0 and r["turns_removed"] == 0 and r["retraction_reasons_redacted"] == 1
    assert r["store_scrubbed"] is not None and _counts(db) == (0, 0)
    with Store(db) as s:
        (reason,) = s.conn.execute("SELECT reason FROM retraction").fetchone()
    assert "(value withheld)" in reason and VALUE not in reason


@pytest.fixture()
def short_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lock or a reader stops the scrub in a tenth of a second instead of a minute."""
    monkeypatch.setattr(store_module, "BUSY_TIMEOUT_MS", 100)
    monkeypatch.setattr(store_module, "CHECKPOINT_WAIT_MS", 100)


def test_a_scrub_blocked_by_a_lock_reports_what_was_removed_and_how_to_finish(
    tmp_path, capsys, monkeypatch, short_waits
):
    db = _db(tmp_path)
    locker = sqlite3.connect(db, isolation_level=None)
    real = Store.scrub

    def scrub_under_a_lock(self, progress=None):
        locker.execute("BEGIN IMMEDIATE")  # another writer takes the lock once the delete commits
        return real(self, progress)

    monkeypatch.setattr(Store, "scrub", scrub_under_a_lock)
    try:
        code, out, err = _run(
            capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply"
        )
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert code == 1
    assert "turns removed : 1" in out
    assert "store file : NOT scrubbed: OperationalError: database is locked" in out
    assert "the scrub did not finish (OperationalError: database is locked)" in err
    assert f"memware --db {db} prune --scrub" in err
    assert VALUE not in out + err

    monkeypatch.setattr(Store, "scrub", real)
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--scrub")
    assert code == 0 and "store file : scrubbed in " in out
    assert _counts(db) == (0, 0)


def test_a_vacuum_that_fails_is_reported_and_the_prune_exits_non_zero(
    tmp_path, capsys, monkeypatch
):
    db = _db(tmp_path)
    real = Store.scrub
    pending = []

    def scrub_with_a_statement_open(self, progress=None):
        def step(message):
            if progress:
                progress(message)
            if "VACUUM" in message:  # VACUUM refuses to run while a statement is in progress
                pending.append(self.conn.execute("SELECT id FROM passage"))

        return real(self, step)

    monkeypatch.setattr(Store, "scrub", scrub_with_a_statement_open)
    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply", "--json"
    )
    r = json.loads(out)
    assert code == 1 and r["turns_removed"] == 1 and r["store_scrubbed"] is None
    assert "cannot VACUUM" in r["scrub_error"]
    assert "the scrub did not finish" in err and "prune --scrub" in err


def test_a_reader_mid_read_leaves_copies_in_the_file_and_the_prune_names_them(
    tmp_path, capsys, short_waits
):
    """Review of #40: with another connection mid-read, the checkpoint cannot move the rewritten
    pages into the file. The file, not the log, still holds the value; the prune says so from a
    check of the file, and exits 1."""
    db = _db(tmp_path)
    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM turn").fetchall()
    try:
        code, out, err = _run(
            capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply", "--json"
        )
        r = json.loads(out)
        assert code == 1 and r["store_scrubbed"]["wal_truncated"] is False
        left = r["left_in_store"]
        assert left["leftover"] and left["occurrences"] == _counts(db)[0] > 0
        assert "still waiting in the write-ahead log" in err and "prune --scrub" in err
    finally:
        reader.execute("COMMIT")
        reader.close()
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--scrub")
    assert code == 0 and _counts(db) == (0, 0) and _counts(_wal(db)) == (0, 0)


def test_an_applied_prune_that_matches_nothing_scrubs_only_what_is_left(tmp_path, capsys):
    """A clean store is not rewritten again; one an older prune left holding the value is."""
    db = _db(tmp_path)
    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", "NOWHERE9", "--apply"
    )
    assert code == 0 and "scrubbing" not in err
    assert "store file : not rewritten: nothing was removed" in out
    assert "text left in the store : nothing" in out

    with Store(db) as s:
        s.conn.execute("PRAGMA secure_delete=OFF")
        s.conn.execute("DELETE FROM turn WHERE instr(text, ?) > 0", (VALUE,))  # 0.6.1's prune
    assert _counts(db) != (0, 0)
    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply", "--json"
    )
    r = json.loads(out)
    assert code == 0 and r["turns_removed"] == 0 and r["store_scrubbed"] is not None
    assert _counts(db) == (0, 0)


def test_a_writer_that_arrives_during_a_scrub_waits_and_lands(tmp_path, capsys, monkeypatch):
    """A hook's sync or a Hermes belief write that arrives mid-scrub must wait for the lock, not
    fail with "database is locked" and lose its write."""
    import threading

    db = _db(tmp_path)
    real = Store.scrub
    errors: list[BaseException] = []
    threads: list[threading.Thread] = []

    def write():
        try:
            with Store(db) as w:
                assert_belief(w, "hermes memory", "note", "written during a scrub", reliability=0.9)
        except BaseException as e:
            errors.append(e)

    def scrub_with_a_writer(self, progress=None):
        def step(message):
            if progress:
                progress(message)
            if "VACUUM" in message:
                threads.append(threading.Thread(target=write))
                threads[-1].start()

        return real(self, step)

    monkeypatch.setattr(Store, "scrub", scrub_with_a_writer)
    code, _, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply")
    threads[0].join(timeout=30)
    assert code == 0 and errors == []
    with Store(db) as s:
        assert (
            s.conn.execute(
                "SELECT count(*) FROM belief WHERE value='written during a scrub'"
            ).fetchone()[0]
            == 1
        )


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
    assert "text left in the store : " in out and "2 beliefs" in out
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
    assert len(progress) == 3  # both indexes merged, then VACUUM, each announced before it runs
    assert "store file : scrubbed in " in out and "write-ahead log emptied" in out
    assert "text left in the store : nothing" in out
    assert str(dest) in err and "`memware scan --backups`" in err
    assert VALUE not in out + err  # the value is never echoed back
    assert not dest.exists()  # a prune never writes to a backup destination


def test_prune_json_reports_the_scrub_and_the_check(tmp_path, capsys):
    db = _db(tmp_path)
    code, out, _ = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply", "--json"
    )
    r = json.loads(out)
    assert r["store_scrubbed"]["indexes"] == ["passage_fts", "belief_fts"]
    assert r["store_scrubbed"]["rebuilt"] == [] and r["store_scrubbed"]["wal_truncated"] is True
    assert r["scrub_error"] is None and r["retraction_reasons_redacted"] == 0
    left = r["left_in_store"]
    assert (left["occurrences"], left["occurrences_any_case"], left["leftover"]) == (0, 0, False)
    assert left["deleted_tokens"] == {"passage_fts": 0, "belief_fts": 0}


@pytest.mark.parametrize("flag", ["--turns-containing", "--turns-starting-with", "--containing"])
def test_a_text_selector_reads_its_text_from_a_file_or_a_hidden_prompt(
    tmp_path, capsys, monkeypatch, flag
):
    db = _db(tmp_path)
    secret = tmp_path / "value.txt"
    secret.write_text(VALUE + "\n")
    text = "the api token is" if flag == "--turns-starting-with" else VALUE
    if flag == "--turns-starting-with":
        secret.write_text(text + "\n")
    code, out, err = _run(
        capsys, "--db", str(db), "prune", flag, "--value-file", str(secret), "--json"
    )
    assert (
        code == 0
        and json.loads(out)["turns_removed" if flag != "--containing" else "sources_pruned"] == 1
    )
    assert text not in out + err

    asked = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt, stream=None: asked.append(prompt) or text)
    code, out, err = _run(capsys, "--db", str(db), "prune", flag, "--json")
    assert code == 0 and asked == ["text to remove (not shown): "]
    assert text not in out + err


def test_value_file_needs_exactly_one_bare_selector(tmp_path, capsys):
    db = _db(tmp_path)
    secret = tmp_path / "value.txt"
    secret.write_text(VALUE)
    code, _, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--value-file", str(secret)
    )
    assert code == 2 and "--value-file" in err
    code, _, err = _run(
        capsys,
        "--db",
        str(db),
        "prune",
        "--turns-containing",
        "--value-file",
        str(tmp_path / "gone"),
    )
    assert code == 2 and "--value-file could not be read" in err


def test_scrub_takes_no_selector(tmp_path, capsys):
    db = _db(tmp_path)
    code, _, err = _run(capsys, "--db", str(db), "prune", "--scrub", "--glob", "*")
    assert code == 2 and "takes no selector" in err


def test_an_index_a_merge_leaves_holding_deleted_terms_is_rebuilt(tmp_path, monkeypatch):
    """The merge is checked, not trusted: if it leaves a term no live row holds, the index is
    rebuilt from its content table."""
    db = _db(tmp_path)
    checked = []

    def once_left(conn, table, terms=None):
        checked.append(table)
        return 1 if checked.count(table) == 1 and table == "passage_fts" else 0

    monkeypatch.setattr(store_module, "deleted_terms", once_left)
    steps: list[str] = []
    with Store(db) as s:
        r = prune(s, turns_containing=VALUE, apply=True, progress=steps.append)
    assert r.scrubbed is not None and r.scrubbed.rebuilt == ("passage_fts",)
    assert any(step.startswith("rebuilding the passage_fts search index") for step in steps)
    assert _counts(db) == (0, 0)
