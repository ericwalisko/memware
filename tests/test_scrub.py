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
from memware.ledger import Policy, assert_belief, make_key
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
        assert "could not empty the write-ahead log" in err and "prune --scrub" in err
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


def _links(db: Path) -> dict[int, tuple]:
    with Store(db) as s:
        return {
            r["id"]: (r["valid_from"], r["valid_to"], r["superseded_by"], r["reliability"])
            for r in s.conn.execute("SELECT * FROM belief")
        }


def test_the_dry_run_lists_the_beliefs_to_redact_by_id_and_apply_redacts_them(tmp_path, capsys):
    """Eric's decision on #40: a prune redacts the text in every belief that holds it, a person's
    included, and retracts the committed ones. Ids and links stay; nothing is deleted."""
    db = _db(tmp_path)
    with Store(db) as s:
        stated = assert_belief(s, "deploy", "api token", f"is {VALUE}", reliability=0.9).belief_id
        subject = assert_belief(s, f"{VALUE} rotation", "is due", "on friday").belief_id
        other = assert_belief(s, "deploy", "region", "us-east-1").belief_id
    before, links = _dump_beliefs(db), _links(db)

    code, out, err = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--json")
    r = json.loads(out)
    assert code == 0 and r["beliefs_redacted"] == [stated, subject]
    assert r["beliefs_retracted_by_redaction"] == [stated, subject]
    code, out, err = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE)
    assert f"beliefs to redact : 2 (ids {stated}, {subject}); 2 committed, to retract" in out
    assert VALUE not in out + err and _dump_beliefs(db) == before  # a dry run writes nothing

    code, out, err = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply")
    assert (
        code == 0
        and f"beliefs redacted : 2 (ids {stated}, {subject}); 2 committed, now retracted" in out
    )
    after = _dump_beliefs(db)
    assert after.keys() == before.keys() and _links(db) == links  # no row deleted, no link moved
    assert (after[stated]["value"], after[stated]["status"]) == ("is [removed]", "retracted")
    assert after[subject]["subject"] == "[removed] rotation"
    assert after[subject]["key"] == make_key("[removed] rotation", "is due")  # rekeyed
    assert after[other] == before[other]
    with Store(db) as s:
        reasons = dict(s.conn.execute("SELECT belief_id, reason FROM retraction").fetchall())
    assert reasons == {
        stated: "memware prune: text redacted (value withheld)",
        subject: reasons[subject],
    }
    assert _counts(db) == (0, 0)
    code, out, _ = _run(capsys, "--db", str(db), "beliefs", "--json")
    assert [b["id"] for b in json.loads(out)] == [other]


def _dump_beliefs(db: Path) -> dict[int, dict]:
    with Store(db) as s:
        return {r["id"]: dict(r) for r in s.conn.execute("SELECT * FROM belief")}


def test_a_prefix_selector_redacts_only_a_leading_match(tmp_path, capsys):
    db = _db(tmp_path)
    prefix = "the api token is"
    with Store(db) as s:
        leading = assert_belief(s, "note", "first", f"{prefix} {VALUE}").belief_id
        inside = assert_belief(s, "note", "second", f"we said {prefix} rotated").belief_id
    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-starting-with", prefix, "--apply"
    )
    beliefs = _dump_beliefs(db)
    assert code == 0 and beliefs[leading]["value"] == f"[removed] {VALUE}"
    assert beliefs[inside]["value"] == f"we said {prefix} rotated"
    assert "1 belief holds the text past the start of a field" in err


def test_redaction_merges_nothing_when_it_makes_two_rows_identical(tmp_path, capsys):
    db = _db(tmp_path)
    with Store(db) as s:
        a = assert_belief(s, "svc", "token", VALUE, policy=Policy.AUTO).belief_id
        b = assert_belief(s, "svc", "token", "[removed]", policy=Policy.AUTO).belief_id
    code, _, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply")
    beliefs = _dump_beliefs(db)
    assert code == 0 and set(beliefs) >= {a, b} and beliefs[a]["value"] == beliefs[b]["value"]


def plant_everywhere(db: Path) -> dict[str, int]:
    """Put the value in every place in the store it can reach in practice, besides the turn the
    corpus holds: a derived belief, a person's belief and its free-text source, a superseded
    predecessor, a belief already retracted, a retraction reason an older prune wrote, a pending
    review's candidate, a rejected candidate, and a confirmation's source. Returns the ids."""
    from memware.ledger import reject

    ids = {}
    with Store(db) as s:
        turn = s.conn.execute(
            "SELECT id, session FROM turn WHERE instr(text, ?) > 0", (VALUE,)
        ).fetchone()
        pointer = source_pointer(turn["session"], turn["id"])
        ids["derived"] = assert_belief(s, "api", "token", f"is {VALUE}", source=pointer).belief_id
        ids["stated"] = assert_belief(
            s, f"{VALUE} vault", "owned by", "platform", reliability=0.9, source=f"said {VALUE}"
        ).belief_id
        ids["predecessor"] = assert_belief(
            s, "db", "password", f"old {VALUE}", valid_from="2026-01-01T00:00:00Z", reliability=0.9
        ).belief_id
        ids["successor"] = assert_belief(
            s, "db", "password", "rotated", valid_from="2026-02-01T00:00:00Z", reliability=0.9
        ).belief_id
        ids["retracted"] = assert_belief(s, "old", "secret", f"was {VALUE}").belief_id
        ids["older_prune"] = assert_belief(s, "old", "session fact", "plain").belief_id
        for key, reason in (
            ("retracted", "manual"),
            ("older_prune", f"memware prune --turns-containing {VALUE!r}"),
        ):
            s.conn.execute(
                "UPDATE belief SET status='retracted', valid_to=valid_from WHERE id=?", (ids[key],)
            )
            s.conn.execute(
                "INSERT INTO retraction(belief_id, retracted_at, reason) VALUES (?, '2026-03-01T00:00:00Z', ?)",
                (ids[key], reason),
            )
        assert_belief(s, "cache", "ttl", "60", reliability=0.9)
        pending = assert_belief(s, "cache", "ttl", f"{VALUE} seconds", reliability=0.2)
        ids["candidate"], ids["review"] = pending.belief_id, pending.review_id
        assert_belief(s, "queue", "size", "10", reliability=0.9)
        rejected = assert_belief(s, "queue", "size", f"{VALUE} items", reliability=0.2)
        reject(s, rejected.review_id)
        ids["rejected"] = rejected.belief_id
        derived_port = assert_belief(s, "port", "is", "5432", source=pointer).belief_id
        assert_belief(s, "port", "is", "5432", source=f"confirmed {VALUE}", reliability=0.9)
        ids["confirmed"] = derived_port
    return ids


def test_a_value_planted_everywhere_it_can_reach_leaves_the_store(tmp_path, capsys):
    """After ``prune --apply``, the store check reports the value nowhere: not in the file, its
    log or its search index, in no row of any table, in any case."""
    from memware.residue import check_file

    db = _db(tmp_path)
    ids = plant_everywhere(db)
    with Store(db) as s:
        assert (
            s.conn.execute(
                "SELECT count(*) FROM confirmation WHERE instr(source, ?)", (VALUE,)
            ).fetchone()[0]
            == 1
        )
        statuses = {
            k: s.conn.execute("SELECT status FROM belief WHERE id=?", (v,)).fetchone()[0]
            for k, v in ids.items()
            if k != "review"
        }
    assert statuses["rejected"] == "rejected" and statuses["candidate"] == "candidate"
    before = check_file(db, VALUE)
    assert before.beliefs == 6 and before.other_rows == {
        "passage.text": 1,
        "confirmation.source": 1,
        "retraction.reason": 1,
    }
    links = _links(db)

    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", VALUE, "--apply", "--json"
    )
    r = json.loads(out)
    assert code == 0 and r["turns_removed"] == 1 and r["retraction_reasons_redacted"] == 1
    assert r["confirmation_sources_redacted"] == [ids["confirmed"]]
    left = check_file(db, VALUE)
    assert (left.occurrences, left.occurrences_any_case, left.wal_occurrences or 0) == (0, 0, 0)
    assert (left.rows, left.turns_any_case, left.beliefs_any_case, left.other_rows) == (0, 0, 0, {})
    assert left.index_tokens == {"passage_fts": 0, "belief_fts": 0} == left.deleted_tokens
    assert not left.found and _links(db) == links
    beliefs = _dump_beliefs(db)
    assert beliefs[ids["candidate"]]["status"] == "candidate"  # a pending review stays pending
    assert beliefs[ids["rejected"]]["status"] == "rejected"
    assert beliefs[ids["stated"]]["status"] == "retracted"  # a person's belief too
    with Store(db) as s:
        reasons = dict(s.conn.execute("SELECT belief_id, reason FROM retraction").fetchall())
    assert reasons[ids["retracted"]] == "manual"  # already retracted: keeps its own retraction


def test_a_glob_prune_counts_no_beliefs(tmp_path, capsys):
    db = _db(tmp_path)
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--glob", "*/t.jsonl", "--json")
    assert code == 0 and "beliefs_holding_text" not in json.loads(out)


def test_a_glob_prune_checks_the_index_after_its_scrub(tmp_path, capsys):
    """A glob has no text to look for, so the check after the scrub counts every term on the index
    pages that no live row holds: what an un-index leaves in the FTS5 segments."""
    db = _db(tmp_path)
    code, out, _ = _run(
        capsys, "--db", str(db), "prune", "--glob", "*/t.jsonl", "--apply", "--json"
    )
    r = json.loads(out)
    assert code == 0 and r["sources_pruned"] == 1 and r["left_in_store"] is None
    assert r["left_in_index"] == {"passage_fts": 0, "belief_fts": 0}
    assert _counts(db) == (0, 0)

    code, out, _ = _run(capsys, "--db", str(db), "prune", "--glob", "*/t.jsonl", "--apply")
    assert code == 0 and "left in the search index" not in out  # nothing removed, no scrub


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


def _hermes_provider(tmp_path: Path, db: Path):
    """The in-tree Hermes provider, loaded as its own tests load it, on ``db``."""
    import importlib.util

    plugin = Path(__file__).resolve().parents[1] / "integrations" / "hermes" / "memware"
    spec = importlib.util.spec_from_file_location("memware_hermes_plugin", plugin / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "memware.json").write_text(json.dumps({"db_path": str(db)}))
    provider = mod.MemwareProvider()
    provider.initialize("sess-scrub", hermes_home=str(home))
    return provider


def test_prefetch_during_a_scrub_with_a_reader_held_stays_fast_and_a_belief_still_lands(
    tmp_path, monkeypatch
):
    """Second review of #40: emptying the log waited for a reader while holding the write lock,
    and Hermes prefetch, which records uses before every turn, waited behind it (14 s at 150,000
    turns). The scrub now holds no lock while it waits, and a use count never waits long."""
    import threading
    import time

    monkeypatch.setattr(store_module, "CHECKPOINT_WAIT_MS", 1500)
    db = _db(tmp_path)
    with Store(db) as s:
        assert_belief(s, "deploy", "region", "us-east-1")
    provider = _hermes_provider(tmp_path, db)
    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM turn").fetchall()
    compacting, pruned = threading.Event(), {}

    def run_prune():
        with Store(db) as s:
            pruned["r"] = prune(
                s,
                turns_containing=VALUE,
                apply=True,
                progress=lambda step: "VACUUM" in step and compacting.set(),
            )

    worker = threading.Thread(target=run_prune)
    worker.start()
    try:
        assert compacting.wait(30)
        time.sleep(0.2)  # VACUUM of a small store is done: the scrub is emptying the log
        slowest, calls, wrote = 0.0, 0, False
        while worker.is_alive():
            started = time.perf_counter()
            assert "us-east-1" in provider.prefetch("deploy region")
            slowest, calls = max(slowest, time.perf_counter() - started), calls + 1
            if not wrote:
                with Store(db) as w:
                    assert_belief(w, "hermes memory", "note", "written while the log waits")
                wrote = True
            time.sleep(0.05)
    finally:
        worker.join()
        reader.execute("COMMIT")
        reader.close()
    assert calls >= 5 and slowest < 1.0, (calls, slowest)
    assert pruned["r"].scrubbed is not None and not pruned["r"].scrubbed.wal_truncated
    with Store(db) as s:
        assert (
            s.conn.execute(
                "SELECT count(*) FROM belief WHERE value='written while the log waits'"
            ).fetchone()[0]
            == 1
        )


def test_a_use_count_is_skipped_rather_than_waited_for(tmp_path):
    db = _db(tmp_path)
    with Store(db) as s:
        assert_belief(s, "deploy", "region", "us-east-1")
    locker = sqlite3.connect(db, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        import time

        from memware.index import search_beliefs

        with Store(db, busy_timeout_ms=store_module.SHORT_WAIT_MS) as s:
            started = time.perf_counter()
            hits = search_beliefs(s, "deploy region", k=3)  # records a use: the lock is held
            took = time.perf_counter() - started
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    # a quarter second of wait plus the search: past 1 s on a macOS CI runner under coverage, and
    # nowhere near the minute a use count waited before
    assert [h.text for h in hits] == ["deploy region us-east-1"] and took < 3.0
    with Store(db) as s:
        assert s.conn.execute("SELECT use_count FROM belief").fetchone()[0] == 0  # skipped


@pytest.mark.parametrize("flag", ["--turns-containing", "--turns-starting-with", "--containing"])
def test_a_selector_value_file_is_one_line(tmp_path, capsys, flag):
    db = _db(tmp_path)
    text = "the api token is" if flag == "--turns-starting-with" else VALUE
    value = tmp_path / "value.txt"
    value.write_text(text + "\n\n")
    code, out, _ = _run(
        capsys, "--db", str(db), "prune", flag, "--value-file", str(value), "--json"
    )
    key = "sources_pruned" if flag == "--containing" else "turns_removed"
    assert code == 0 and json.loads(out)[key] == 1

    value.write_text("\n" + text + "\n")
    code, out, err = _run(
        capsys, "--db", str(db), "prune", flag, "--value-file", str(value), "--apply"
    )
    assert code == 2 and out == "" and "holds a line break" in err
    assert _counts(db)[0] > 0  # nothing was removed


def test_pruning_a_value_another_live_turn_holds_in_another_case_is_not_a_leftover(
    tmp_path, capsys
):
    """Second review of #40: pruning ``hunter2`` while a live turn says ``Hunter2`` left the term
    ``hunter2`` on an index page for that turn. The check counted it as a copy, exited 1, and
    every later prune scrubbed again."""
    root = tmp_path / "corpus"
    root.mkdir()
    lines = [
        {"role": "user", "content": "the wifi password is hunter2 for the guest network"},
        {"role": "user", "content": "Hunter2 was the password of the old router"},
    ]
    (root / "c.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    db = tmp_path / "case.db"
    with Store(db) as s:
        sync_tree(s, root, harness="generic")

    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", "hunter2", "--apply"
    )
    assert code == 0 and "turns removed : 1" in out
    assert "text left in the store : " in out and "1 turn in another case" in out
    assert "1 turn or belief holds the text in another case" in err and "prune --scrub" not in err

    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", "hunter2", "--apply", "--json"
    )
    r = json.loads(out)
    assert code == 0 and r["store_scrubbed"] is None and "scrubbing" not in err  # not again
    left = r["left_in_store"]
    assert left["leftover"] is False and left["occurrences"] > 0  # the live turn's index term
    assert (left["turns"], left["turns_any_case"]) == (0, 1)
    assert left["live_term_rows"]["passage_fts"] == 1 and left["term_is_value"] is True


@pytest.mark.parametrize(
    ("argv", "limit"),
    [
        (["context", "--from-hook"], 10),
        (["digest", "--from-hook"], 5),
        (["notice", "--from-hook"], 5),
    ],
)
def test_a_hook_opening_a_store_mid_upgrade_under_a_held_lock_returns_in_time(
    tmp_path, capsys, monkeypatch, argv, limit
):
    """Merged with #39: the first open after the upgrade creates the ``notice`` and
    ``confirmation`` tables, which takes the write lock. With another writer holding it, each hook
    waited 12 s, past its timeout. A hook now waits a quarter second, says nothing this time, and
    the next open creates the tables."""
    import io
    import time

    db = _db(tmp_path)
    with Store(db) as s:
        assert_belief(s, "deploy", "region", "us-east-1")
        s.conn.execute("DROP TABLE notice")  # a store written before #39
        s.conn.execute("DROP TABLE confirmation")
    payload = {
        "prompt": "which deploy region",
        "cwd": str(tmp_path),
        "session_id": "s1",
        "source": "startup",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    locker = sqlite3.connect(db, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        started = time.perf_counter()
        code = main(["--db", str(db), *argv])
        took = time.perf_counter() - started
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    out = capsys.readouterr()
    assert code == 0 and took < 3.0 < limit, (
        took
    )  # well inside the hook's timeout, on a slow runner too
    assert "Traceback" not in out.err

    code, _, _ = _run(capsys, "--db", str(db), "beliefs")  # an ordinary command upgrades it
    with Store(db) as s:
        tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert code == 0 and {"notice", "confirmation"} <= tables
