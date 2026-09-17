"""``memware scan``: where a value is still stored, read-only.

Pins #37: ``prune --containing`` reads only indexed sources, so a transcript kept out of the
index (by ``capture.exclude`` above all) was invisible to the one command that reads transcript
files. And the store half of #36: a deleted turn's search terms stay on FTS5 pages that
``fts5vocab`` does not show, prefix-compressed so a byte search misses them too. Every store and
transcript here is synthetic, under the test's own tmp dir.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
from pathlib import Path

import pytest

from memware import backup as bk
from memware.cli import main
from memware.ingest import file_occurrences, prune, record_no_capture, sync_tree
from memware.ledger import assert_belief
from memware.scan import (
    _LEAF_ROWID_MIN,
    FTS_TABLES,
    TOKENIZER,
    index_holds,
    leaf_terms,
    scan,
    value_tokens,
)
from memware.store import SCHEMA, Store

VALUE = "HUNTER2SECRET"
CONTEXT = "the api token is"  # text beside the value, which scan must never print
ABSENT = "ABSENTVALUE9431"  # one search term, in no file


def _write(path: Path, texts: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps({"role": "user", "content": t}) + "\n" for t in texts),
        encoding="utf-8",
    )
    return path


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """A transcript source with one indexed transcript holding the value once, and one ordinary;
    the store synced from it; the config pointing at both."""
    src = tmp_path / "projects"
    _write(src / "-work" / "kept.jsonl", [f"{CONTEXT} {VALUE} and must not persist", "x" * 40])
    _write(src / "-work" / "clean.jsonl", ["an ordinary line with nothing in it at all"])
    db = tmp_path / "home" / "memware.db"
    _config(db, "backup.transcript_src", str(src))
    with Store(db) as s:
        sync_tree(s, src, harness="generic")
    return src, db


def _config(db: Path, key: str, value: str) -> None:
    assert main(["--db", str(db), "config", key, value]) == 0


def _scan(capsys, db: Path, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(["--db", str(db), "scan", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def _json(capsys, db: Path, *argv: str) -> tuple[int, dict]:
    code, out, _ = _scan(capsys, db, *argv, "--json")
    return code, json.loads(out)


def test_a_value_in_an_excluded_transcript_is_found_and_reported_excluded(tmp_path, capsys):
    """#37: the transcript capture.exclude keeps out of the index is the one scan must read."""
    src, db = _setup(tmp_path)
    _write(src / "-gen" / "run.jsonl", [f"{CONTEXT} {VALUE}", f"again {VALUE} and {VALUE}"])
    _config(db, "capture.exclude", "*/-gen/*")
    with Store(db) as s:
        sync_tree(s, src, harness="generic")
        # prune reads only indexed sources, so it cannot see the excluded file
        assert list(prune(s, containing=VALUE).sources) == [str(src / "-work" / "kept.jsonl")]

    code, r = _json(capsys, db, VALUE)
    assert code == 1 and r["found"] and r["complete"]
    assert r["transcripts_read"] == 3
    hits = {Path(h["path"]).name: h for h in r["transcripts"]}
    assert hits["run.jsonl"] == {
        "path": str(src / "-gen" / "run.jsonl"),
        "occurrences": 3,
        "indexed": False,
        "excluded_by": "capture.exclude",
    }
    assert hits["kept.jsonl"]["indexed"] is True and hits["kept.jsonl"]["excluded_by"] is None
    assert "clean.jsonl" not in hits

    code, out, err = _scan(capsys, db, VALUE)
    assert code == 1 and err == ""
    assert "verdict : found in 2 transcripts, the store file" in out
    assert "indexed : no: excluded by capture.exclude" in out
    assert VALUE not in out and CONTEXT not in out  # paths and counts only


def test_why_a_transcript_is_not_indexed(tmp_path, capsys, monkeypatch):
    src, db = _setup(tmp_path)
    listed = _write(src / "-a" / "listed.jsonl", [f"{VALUE} in a no-capture session"])
    record_no_capture(listed)
    _write(src / "-a" / "marked.jsonl", [f"[memware-eval] {VALUE} in an eval run"])
    monkeypatch.setenv("MEMWARE_IGNORE_MARKERS", "[memware-eval]")
    _write(src / "-a" / "new.jsonl", [f"{VALUE} in a session no sync has reached"])
    _config(db, "capture.exclude", "*/kept.jsonl")  # indexed before the pattern was added

    code, r = _json(capsys, db, VALUE)
    assert code == 1
    why = {Path(h["path"]).name: (h["indexed"], h["excluded_by"]) for h in r["transcripts"]}
    assert why == {
        "listed.jsonl": (False, "no-capture list"),
        "marked.jsonl": (False, "ignore marker"),
        "new.jsonl": (False, None),
        "kept.jsonl": (True, "capture.exclude"),
    }
    _, out, _ = _scan(capsys, db, VALUE)
    assert "indexed : no: not synced yet" in out
    assert "indexed : yes, and excluded by capture.exclude: the next sync un-indexes it" in out


def test_nothing_found_exits_zero(tmp_path, capsys):
    _, db = _setup(tmp_path)
    code, out, _ = _scan(capsys, db, ABSENT)
    assert code == 0 and "verdict : not found" in out
    code, r = _json(capsys, db, ABSENT)
    assert code == 0 and not r["found"] and r["complete"]
    assert r["store_check"]["found"] is False and r["store_check"]["index_tokens"] == {
        "passage_fts": 0,
        "belief_fts": 0,
    }


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_a_file_it_cannot_read_is_listed_and_the_scan_is_not_clean(tmp_path, capsys):
    src, db = _setup(tmp_path)
    locked = _write(src / "-b" / "locked.jsonl", ["whatever it holds is unknown"])
    locked_dir = src / "-c"
    _write(locked_dir / "inside.jsonl", ["nor this"])
    locked.chmod(0)
    locked_dir.chmod(0)
    try:
        code, r = _json(capsys, db, ABSENT)
        assert code == 2 and not r["found"] and not r["complete"]
        unread = {Path(u["path"]).name: u["reason"] for u in r["unread"]}
        assert unread == {"locked.jsonl": "Permission denied", "-c": "Permission denied"}
        _, out, _ = _scan(capsys, db, ABSENT)
        assert "verdict : not found, but 2 paths could not be read: see below" in out
        assert f"not read : {locked}" in out

        code, _ = _json(capsys, db, VALUE)  # found somewhere else: that decides the exit
        assert code == 1
    finally:
        locked.chmod(0o644)
        locked_dir.chmod(0o755)


def test_an_indexed_source_whose_file_is_gone_is_counted_not_unread(tmp_path, capsys):
    src, db = _setup(tmp_path)
    (src / "-work" / "clean.jsonl").unlink()
    code, r = _json(capsys, db, ABSENT)
    assert code == 0 and r["complete"]
    assert r["indexed_gone"] == [str((src / "-work" / "clean.jsonl").resolve())]


def test_an_indexed_source_outside_the_transcript_source_is_read(tmp_path, capsys):
    src, db = _setup(tmp_path)
    elsewhere = _write(tmp_path / "exports" / "chat.jsonl", [f"exported {VALUE} line here"])
    with Store(db) as s:
        sync_tree(s, elsewhere.parent, harness="generic")
    _, r = _json(capsys, db, VALUE)
    hit = next(h for h in r["transcripts"] if h["path"] == str(elsewhere.resolve()))
    assert hit["indexed"] is True and hit["occurrences"] == 1


def test_the_store_check_finds_index_terms_that_fts5vocab_and_the_bytes_miss(tmp_path, capsys):
    """#36 from scan's side: a turn deleted as 0.6.1 deleted it, with no rebuild. The term just
    before the value's shares its prefix, in the live segment and in the delete's own, so each page
    stores the value's term as a two-byte suffix that neither a byte search nor fts5vocab sees."""
    src = tmp_path / "projects"
    neighbours = [f"hunter{n} hunter2secra hunter2secrex hunter2secreu" for n in range(300)]
    _write(src / "s.jsonl", [*neighbours, f"{CONTEXT} hunter2secra {VALUE} and must not persist"])
    db = tmp_path / "home" / "memware.db"
    with Store(db) as s:
        sync_tree(s, src, harness="generic")
        s.conn.execute("PRAGMA secure_delete=ON")
        s.conn.execute("DELETE FROM turn WHERE instr(text, ?) > 0", (VALUE,))
        s.conn.execute("CREATE VIRTUAL TABLE temp.v USING fts5vocab(main, passage_fts, row)")
        vocab = s.conn.execute("SELECT count(*) FROM temp.v WHERE term = ?", (VALUE.lower(),))
        assert vocab.fetchone()[0] == 0  # the logical index says the term is gone
    data = db.read_bytes()
    assert VALUE.encode() not in data and VALUE.lower().encode() not in data  # so do the bytes

    code, r = _json(capsys, db, VALUE, "--transcript-src", str(tmp_path / "empty"))
    check = r["store_check"]
    assert check["found"] and check["tokens"] == 1
    assert check["index_tokens"] == {"passage_fts": 1, "belief_fts": 0}
    assert (check["turns"], check["occurrences"], check["occurrences_any_case"]) == (0, 0, 0)
    assert code == 1

    with Store(db) as s:
        prune(s, turns_containing=VALUE, apply=True)
    _, r = _json(capsys, db, VALUE, "--transcript-src", str(tmp_path / "empty"))
    assert r["store_check"]["index_tokens"] == {"passage_fts": 0, "belief_fts": 0}
    assert not r["store_check"]["found"] and r["store_check"]["free_pages"] == 0


def test_the_store_check_counts_beliefs_and_the_log(tmp_path, capsys):
    _, db = _setup(tmp_path)
    with Store(db) as s:
        assert_belief(s, "deploy", "api token", VALUE)
    holder = sqlite3.connect(db)  # keeps the -wal file in place after the store closes
    holder.execute(
        "SELECT count(*) FROM belief"
    ).fetchall()  # a read opens the log; SELECT 1 does not
    try:
        with Store(db) as s:
            assert_belief(s, "deploy", "old token", f"was {VALUE}")
        _, r = _json(capsys, db, VALUE)
    finally:
        holder.close()
    check = r["store_check"]
    assert check["beliefs"] == 2 and check["turns"] == 1
    assert check["index_tokens"]["belief_fts"] == 1
    assert check["wal_occurrences"] >= 1


def test_backups_reads_mirrored_transcripts_snapshots_and_restore_copies(tmp_path, capsys):
    src, db = _setup(tmp_path)
    dest = tmp_path / "dropbox"
    _config(db, "backup.dest", str(dest))
    bk.mirror_transcripts(src, dest)
    old = bk.snapshot(db, dest)
    with Store(db) as s:
        prune(s, turns_containing=VALUE, apply=True)
    bk.restore(old, db)  # sets the pruned store aside, beside it
    with Store(db) as s:
        prune(s, turns_containing=VALUE, apply=True)
    (src / "-work" / "kept.jsonl").unlink()

    code, r = _json(capsys, db, VALUE)
    assert code == 0 and r["backup_dest"] is None  # the store and transcripts are clean now
    assert [c["found"] for c in r["store_copies"]] == [False]

    code, r = _json(capsys, db, VALUE, "--backups")
    assert code == 1 and r["backup_dest"] == str(dest)
    assert r["mirrored_read"] == 2
    assert [(Path(h["path"]).name, h["occurrences"]) for h in r["mirrored"]] == [("kept.jsonl", 1)]
    assert [(Path(c["path"]).name, c["found"]) for c in r["snapshots"]] == [(old.name, True)]
    snap = r["snapshots"][0]
    assert snap["turns"] == 1 and snap["index_tokens"]["passage_fts"] == 1

    _, out, _ = _scan(capsys, db, VALUE, "--backups")
    assert "verdict : found in 1 mirrored transcript, 1 snapshot" in out
    assert VALUE not in out and CONTEXT not in out


def test_backups_needs_a_destination(tmp_path, capsys):
    _, db = _setup(tmp_path)
    code, out, err = _scan(capsys, db, VALUE, "--backups")
    assert code == 2 and out == "" and "--dest DIR" in err
    code, r = _json(capsys, db, ABSENT, "--dest", str(tmp_path / "unmounted"))
    assert code == 2 and r["unread"] == [
        {"path": str(tmp_path / "unmounted"), "reason": "the backup destination is not a directory"}
    ]


def test_scan_writes_nothing_anywhere(tmp_path, capsys):
    src, db = _setup(tmp_path)
    dest = tmp_path / "dropbox"
    bk.mirror_transcripts(src, dest)
    bk.snapshot(db, dest)

    def state() -> dict[str, str]:
        return {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(tmp_path.rglob("*"))
            if p.is_file()
        }

    before = state()
    assert main(["--db", str(db), "scan", VALUE, "--dest", str(dest), "--json"]) == 1
    capsys.readouterr()
    assert state() == before


def test_the_value_can_come_from_stdin(tmp_path, capsys, monkeypatch):
    import io

    _, db = _setup(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(VALUE + "\n"))
    code, r = _json(capsys, db, "-")
    assert code == 1 and len(r["transcripts"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    code, _, err = _scan(capsys, db, "-")
    assert code == 2 and "empty" in err


def test_a_value_json_escaped_in_the_transcript_is_counted(tmp_path, capsys):
    src, db = _setup(tmp_path)
    tricky = 'pa"ss\\wörd'
    _write(src / "-d" / "escaped.jsonl", [f"the password is {tricky} ok"])  # json.dumps escapes
    raw = src / "-d" / "raw.jsonl"
    raw.write_text(
        json.dumps({"role": "user", "content": f"is {tricky}"}, ensure_ascii=False) + "\n"
    )
    assert tricky.encode() not in raw.read_bytes()
    _, r = _json(capsys, db, tricky)
    assert sorted((Path(h["path"]).name, h["occurrences"]) for h in r["transcripts"]) == [
        ("escaped.jsonl", 1),
        ("raw.jsonl", 1),
    ]


@pytest.mark.parametrize("chunk_bytes", [1, 2, 3, 7, 13, 64])
def test_occurrences_count_once_across_chunk_boundaries(tmp_path, chunk_bytes):
    data = b"xxSECRETyySECRETSECRETzzsecretAAAAA"
    p = tmp_path / "f"
    p.write_bytes(data)
    assert file_occurrences(p, [b"SECRET"], chunk_bytes=chunk_bytes) == 3
    assert file_occurrences(p, [b"SECRET"], any_case=True, chunk_bytes=chunk_bytes) == 4
    assert file_occurrences(p, [b"AA"], chunk_bytes=chunk_bytes) == 4  # overlapping, each counts
    assert file_occurrences(p, [b"SECRET", b"yy", b"y"], chunk_bytes=chunk_bytes) == 6


def test_the_tokenizer_is_the_one_the_schema_declares():
    assert SCHEMA.count(f"tokenize='{TOKENIZER}'") == len(FTS_TABLES)
    assert value_tokens("the API-token is HUNTER2SECRET") == [
        "api",
        "hunter2secret",
        "is",
        "the",
        "token",
    ]
    assert value_tokens("--") == []


def test_leaf_pages_decode_to_exactly_the_terms_fts5vocab_lists(tmp_path):
    """Without a delete, the terms on the pages and the logical vocabulary are the same set. A
    store large enough for many pages per segment, prefix compression and doclists spanning
    pages is the check that the page reader is right."""
    rng = random.Random(37)
    words = [
        "".join(rng.choice("abcdefghij") for _ in range(rng.randint(1, 9))) for _ in range(4000)
    ]
    src = tmp_path / "projects"
    _write(src / "big.jsonl", [" ".join(rng.choices(words, k=60)) for _ in range(1500)])
    db = tmp_path / "big.db"
    with Store(db) as s:
        sync_tree(s, src, harness="generic")
        for i in range(200):
            assert_belief(s, f"svc{i}", rng.choice(words), " ".join(rng.choices(words, k=5)))
        con = s.conn
        for table in FTS_TABLES:
            pages, on_pages = 0, set()
            for rowid, page in con.execute(
                f"SELECT id, block FROM {table}_data WHERE id >= ?", (_LEAF_ROWID_MIN,)
            ):
                if (rowid >> 31) & 0x3F:
                    continue
                pages += 1
                on_pages.update(t[1:].decode() for t in leaf_terms(page) if t[:1] == b"0")
            con.execute(f"CREATE VIRTUAL TABLE temp.v_{table} USING fts5vocab(main, {table}, row)")
            vocab = {r[0] for r in con.execute(f"SELECT term FROM temp.v_{table}")}
            assert on_pages == vocab and len(vocab) > 100
            if table == "passage_fts":
                assert pages > 50
            sample = sorted(vocab)[:: max(1, len(vocab) // 50)]
            assert index_holds(con, table, [*sample, "zz-not-a-term"]) == len(sample)


def test_scan_rejects_an_empty_value(tmp_path):
    with pytest.raises(ValueError):
        scan("", db=tmp_path / "m.db", transcript_src=tmp_path)
