"""``capture.exclude``: path globs in the config that no sync indexes and no backup mirrors.

``MEMWARE_NO_CAPTURE`` reaches only the processes a run starts, and three harnesses forgot it in
two days. A marker needs its text inside the transcript. A pattern lives in the machine's config,
so it excludes a generator by where its sessions run, whatever the generator's author forgot.
``exclude --apply`` scrubs the store file as ``prune --apply`` does, so what it un-indexes leaves
the file too. Every test here runs on a synthetic transcript tree and a scratch ``MEMWARE_HOME``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from memware import backup as bk
from memware.cli import _segment_forms, main
from memware.config import config_path, memware_home
from memware.ingest import (
    capture_exclude_patterns,
    is_excluded,
    matches_exclude,
    record_no_capture,
    sync_file,
)
from memware.ledger import assert_belief, current
from memware.residue import FTS_TABLES, check_file
from memware.store import Scrubbed, Store
from tests.conftest import write_claude_jsonl

GLOB = "*/-Users-me-gen-runs/*"
VALUE = "QUIXOTIC77"


def _config(**capture: object) -> dict:
    """The config the catch-up, backfill and backup read, pointed at the synthetic tree."""
    cfg = json.loads(config_path().read_text()) if config_path().exists() else {}
    cfg.setdefault("capture", {}).update(capture)
    config_path().write_text(json.dumps(cfg))
    return cfg


def _turns_from(db: str | Path, path: Path) -> int:
    with Store(db) as s:
        row = s.conn.execute("SELECT count(*) FROM turn WHERE source=?", (str(path.resolve()),))
        return int(row.fetchone()[0])


@pytest.fixture()
def machine(tmp_path):
    """Claude Code's layout: one project directory per working directory. The generator runs
    from its own, so a glob can name it without touching the interactive project beside it."""
    projects = tmp_path / "projects"
    wiki, gen = projects / "-Users-me-llm-wiki", projects / "-Users-me-gen-runs"
    near = projects / "-Users-me-gen-runs-archive"  # a neighbour the glob must not catch
    for d in (wiki, gen, near):
        d.mkdir(parents=True)
    files = {
        "wiki": wiki / "w1.jsonl",
        "near": near / "n1.jsonl",
        "gen": gen / "g1.jsonl",
        "gen2": gen / "g2.jsonl",
        "subagent": gen / "g1" / "subagents" / "agent-a1.jsonl",
    }
    write_claude_jsonl(
        files["wiki"], "w1", [("user", "2026-09-16T10:00:00Z", "the linkifier skips code fences")]
    )
    write_claude_jsonl(
        files["near"], "n1", [("user", "2026-09-16T10:30:00Z", "archive the old run notes")]
    )
    for name in ("gen", "gen2"):
        write_claude_jsonl(
            files[name],
            files[name].stem,
            [
                ("user", "2026-09-16T11:00:00Z", "distill a skill candidate from the corpus"),
                (
                    "assistant",
                    "2026-09-16T11:00:05Z",
                    "the candidate became a three-step draft skill",
                ),
            ],
        )
    files["subagent"].parent.mkdir(parents=True)
    write_claude_jsonl(
        files["subagent"], "g1", [("assistant", "2026-09-16T11:00:03Z", "examiner verdict: pass")]
    )
    dest = tmp_path / "dropbox" / "memware"
    memware_home().mkdir(parents=True)
    config_path().write_text(
        json.dumps({"backup": {"dest": str(dest), "transcript_src": str(projects)}})
    )
    return {"db": str(tmp_path / "m.db"), "projects": projects, "dest": dest, **files}


def _excluded(machine) -> list[Path]:
    return [machine["gen"], machine["gen2"], machine["subagent"]]


def test_an_excluded_transcript_is_never_indexed_or_mirrored_and_is_counted(machine, capsys):
    db, dest = machine["db"], machine["dest"]
    _config(exclude=[GLOB])
    earlier = dest / "transcripts" / "-Users-me-gen-runs" / "g2.jsonl"  # mirrored before the glob
    earlier.parent.mkdir(parents=True)
    earlier.write_text("an old copy")

    assert main(["--db", db, "sync"]) == 0  # the SessionStart catch-up
    assert main(["--db", db, "backfill", str(machine["projects"])]) == 0
    capsys.readouterr()
    assert main(["--db", db, "backup", "--json"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)

    assert [_turns_from(db, p) for p in _excluded(machine)] == [0, 0, 0]
    assert (_turns_from(db, machine["wiki"]), _turns_from(db, machine["near"])) == (1, 1)
    mirror = dest / "transcripts"
    assert (mirror / "-Users-me-llm-wiki" / "w1.jsonl").exists()
    assert (mirror / "-Users-me-gen-runs-archive" / "n1.jsonl").exists()
    assert not (mirror / "-Users-me-gen-runs" / "g1.jsonl").exists()
    assert not (mirror / "-Users-me-gen-runs" / "g1").exists()
    assert out["transcripts_mirrored"] == 2
    assert out["transcripts_skipped_glob"] == 3
    assert (out["transcripts_skipped_no_capture"], out["transcripts_skipped_marker"]) == (0, 0)
    assert out["transcripts_left_in_backup"] == [str(earlier)]
    assert earlier.read_text() == "an old copy"  # reported, never deleted
    assert "capture.exclude hides 3 of 5 (60.0%) transcripts on disk" in cap.err


@pytest.mark.parametrize("command", ["sync", "backfill"])
def test_a_transcript_indexed_before_it_was_excluded_is_un_indexed(machine, command):
    db = machine["db"]
    assert main(["--db", db, "sync"]) == 0
    assert [_turns_from(db, p) for p in _excluded(machine)] == [2, 2, 1]

    _config(exclude=[GLOB])
    argv = ["sync"] if command == "sync" else ["backfill", str(machine["projects"])]
    assert main(["--db", db, *argv]) == 0

    with Store(db) as s:
        sources = {r[0] for r in s.conn.execute("SELECT source FROM cursor")}
        assert s.stats()["turns"] == 2 and s.stats()["passages"] == 2
    assert sources == {str(machine["wiki"].resolve()), str(machine["near"].resolve())}


def test_sync_file_and_the_mirror_honour_the_config_directly(machine, tmp_path):
    """The Hermes provider calls ``sync_file`` and ``mirror_transcripts`` itself."""
    _config(exclude=GLOB)  # `memware config capture.exclude GLOB` writes a bare string
    with Store(machine["db"]) as s:
        assert sync_file(s, machine["gen"], harness="claude-code") == 0
        assert sync_file(s, machine["wiki"], harness="claude-code") == 1

    res = bk.mirror_transcripts(machine["projects"], tmp_path / "elsewhere")

    assert sorted(res.excluded_glob) == sorted(_excluded(machine))
    assert (res.copied, res.seen) == (2, 5)


def test_each_transcript_is_counted_under_the_first_layer_that_excludes_it(machine, tmp_path):
    _config(exclude=[GLOB])
    record_no_capture(machine["gen"])
    (memware_home() / "ignore-markers.txt").write_text("distill a skill candidate\n")

    res = bk.mirror_transcripts(machine["projects"], tmp_path / "elsewhere")

    # The list covers a session's subagents; the marker heads both generator sessions.
    assert sorted(res.excluded_no_capture) == sorted([machine["gen"], machine["subagent"]])
    assert res.excluded_glob == [machine["gen2"]]
    assert res.excluded_marker == []
    assert res.copied == 2


def test_a_pattern_matches_the_whole_resolved_path(tmp_path, monkeypatch):
    root = tmp_path / "projects"

    def hit(rel: str, pattern: str = GLOB) -> bool:
        return matches_exclude(root / rel, pattern)

    assert hit("-Users-me-gen-runs/s.jsonl")
    assert hit("-Users-me-gen-runs/s/subagents/agent-1.jsonl")  # * crosses /
    assert not hit("-Users-me-gen-runs-archive/s.jsonl")
    assert not hit("-Users-me-llm-wiki/s.jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert matches_exclude(root / "x" / "s.jsonl", "~/projects/x/*")
    assert not is_excluded(root / "x" / "s.jsonl", [])


def test_the_config_value_is_read_leniently(machine):
    assert capture_exclude_patterns() == []
    _config(exclude=["  */a/*  ", "", 7, None, "*/b/*"])
    assert capture_exclude_patterns() == ["*/a/*", "*/b/*"]
    _config(exclude={"not": "a list"})
    assert capture_exclude_patterns() == []


def test_exclude_add_writes_nothing_without_apply(machine, capsys):
    db = machine["db"]
    assert main(["--db", db, "sync"]) == 0
    before = config_path().read_bytes()
    capsys.readouterr()

    assert main(["--db", db, "exclude", "--add", GLOB]) == 0
    human = capsys.readouterr().out
    assert main(["--db", db, "--json", "exclude", "--add", GLOB]) == 0
    out = json.loads(capsys.readouterr().out)

    assert config_path().read_bytes() == before
    assert [_turns_from(db, p) for p in _excluded(machine)] == [2, 2, 1]
    assert (out["action"], out["applied"], out["transcripts"], out["excluded"]) == (
        "add",
        False,
        5,
        3,
    )
    assert out["patterns"] == [
        {"pattern": GLOB, "transcripts": 3, "indexed_sources": 3, "indexed_turns": 5}
    ]
    assert (out["unindex_sources"], out["unindex_turns"]) == (3, 5)
    assert "dry run, nothing written" in human
    assert "run again with --apply to add it to capture.exclude" in human
    assert "capture.exclude hides 3 of 5 (60.0%) transcripts on disk" in human


def test_a_dry_run_does_not_create_a_store(machine, capsys):
    assert main(["--db", machine["db"], "exclude", "--add", GLOB]) == 0
    assert not Path(machine["db"]).exists()


def test_exclude_add_apply_un_indexes_sources_no_sync_would_visit_again(machine, capsys):
    """A transcript the OS already cleaned up is still indexed, and no sync walks to it."""
    db = machine["db"]
    assert main(["--db", db, "sync"]) == 0
    gone = machine["gen2"]
    gone.unlink()
    capsys.readouterr()

    assert main(["--db", db, "--json", "exclude", "--add", GLOB, "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)

    assert capture_exclude_patterns() == [GLOB]
    assert json.loads(config_path().read_text())["backup"]["transcript_src"]  # the rest is kept
    assert (out["applied"], out["unindex_sources"], out["transcripts"]) == (True, 3, 4)
    assert out["beliefs_orphaned"] == 0  # nothing was derived from them
    assert [_turns_from(db, p) for p in _excluded(machine)] == [0, 0, 0]
    assert _turns_from(db, machine["wiki"]) == 1

    assert main(["--db", db, "exclude", "--add", GLOB, "--apply"]) == 0  # idempotent
    assert capture_exclude_patterns() == [GLOB]


def test_exclude_apply_retracts_no_belief_and_counts_the_orphans(machine, capsys):
    """Like a sync skip, un-indexing by pattern leaves derived beliefs for the orphan one-shot."""
    db = machine["db"]
    assert main(["--db", db, "sync"]) == 0
    with Store(db) as s:
        assert_belief(
            s, "skill factory", "drafts", "three steps", source="memware:session/g1/turn/2"
        )
        assert_belief(s, "linkifier", "skips", "code fences", source="memware:session/w1/turn/1")
    capsys.readouterr()

    assert main(["--db", db, "exclude", "--add", GLOB, "--apply"]) == 0
    human = capsys.readouterr().out
    assert main(["--db", db, "--json", "stats"]) == 0
    stats = json.loads(capsys.readouterr().out)

    assert "1 belief cites a session that is no longer indexed" in human
    assert stats["utilization"]["beliefs_orphaned"] == 1
    with Store(db) as s:
        assert {b["subject"] for b in current(s)} == {"skill factory", "linkifier"}


def test_exclude_lists_each_pattern_and_the_union(machine, capsys):
    db = machine["db"]
    _config(exclude=[GLOB, "*/g1/subagents/*", "*/-Users-me-nowhere/*"])
    assert main(["--db", db, "sync"]) == 0
    capsys.readouterr()

    assert main(["--db", db, "--json", "exclude"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert main(["--db", db, "exclude"]) == 0
    human = capsys.readouterr().out

    assert [(r["pattern"], r["transcripts"]) for r in out["patterns"]] == [
        (GLOB, 3),
        ("*/g1/subagents/*", 1),
        ("*/-Users-me-nowhere/*", 0),
    ]
    assert out["excluded"] == 3  # overlapping patterns count a transcript once
    assert out["unindex_sources"] == 0  # the sync already left them out
    assert "list (dry run, nothing written)" in human
    assert "transcripts excluded : 3 of 5 (60.0%)" in human
    assert "--apply" not in human.split("verdict", 1)[-1]  # nothing to apply


def test_exclude_remove_needs_apply_and_the_next_sync_indexes_again(machine, capsys):
    db = machine["db"]
    _config(exclude=[GLOB])
    assert main(["--db", db, "sync"]) == 0
    assert main(["--db", db, "exclude", "--remove", "*/not-there/*"]) == 2
    before = config_path().read_bytes()
    capsys.readouterr()

    assert main(["--db", db, "--json", "exclude", "--remove", GLOB]) == 0
    out = json.loads(capsys.readouterr().out)
    assert config_path().read_bytes() == before
    assert (out["reindexable"], out["patterns"], out["removed"]["transcripts"]) == (3, [], 3)

    assert main(["--db", db, "exclude", "--remove", GLOB, "--apply"]) == 0
    assert capture_exclude_patterns() == []
    assert main(["--db", db, "sync"]) == 0
    assert [_turns_from(db, p) for p in _excluded(machine)] == [2, 2, 1]


def test_exclude_add_and_remove_are_exclusive_and_reject_an_empty_pattern(machine, capsys):
    with pytest.raises(SystemExit):
        main(["exclude", "--add", "a", "--remove", "b"])
    assert main(["--db", machine["db"], "exclude", "--add", "  "]) == 2
    assert not config_path().read_text().count("capture")


def test_a_pattern_that_matches_nothing_says_how_patterns_match(machine, capsys):
    assert main(["--db", machine["db"], "exclude", "--add=-Users-me-gen-runs"]) == 0
    out = capsys.readouterr().out
    assert "the pattern matches no transcript on disk and no indexed source" in out
    assert "matched against the whole resolved path, and `*` crosses `/`" in out
    assert "refuses" not in out  # written as no path, so --apply adds it as it is


@pytest.mark.parametrize(
    ("patterns", "hidden", "warned"),
    [(["*/g1/subagents/*"], "1 of 5 (20.0%)", False), (["*/-Users-me-*"], "5 of 5 (100.0%)", True)],
)
def test_stats_and_backup_call_out_an_exclusion_hiding_most_transcripts(
    machine, capsys, patterns, hidden, warned
):
    """A pattern that names one generator hides a minority; one that also swallowed the
    interactive project beside it hides at least half, and both commands say so."""
    db = machine["db"]
    _config(exclude=patterns)
    Store(db).close()
    capsys.readouterr()

    assert main(["--db", db, "stats"]) == 0
    human = capsys.readouterr().out
    assert main(["--db", db, "--json", "stats"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert main(["--db", db, "backup", "--quiet"]) == 0
    err = capsys.readouterr().err

    assert stats["capture"]["exclude"] == patterns
    assert stats["capture"]["transcripts"] == 5
    assert "capture.exclude : 1 pattern" in human
    assert f"transcripts excluded : {hidden}" in human
    assert (f"verdict : capture.exclude hides {hidden} transcripts" in human) is warned
    assert (f"capture.exclude hides {hidden} transcripts on disk" in err) is warned


def test_stats_walks_no_transcripts_without_a_pattern(machine, capsys, monkeypatch):
    import memware.cli as cli

    def boom(src: str) -> list[str]:
        raise AssertionError("walked the transcript tree with no pattern set")

    monkeypatch.setattr(cli, "_transcripts_on_disk", boom)
    assert main(["--db", machine["db"], "--json", "stats"]) == 0
    capture = json.loads(capsys.readouterr().out)["capture"]
    assert capture == {"exclude": [], "transcripts": None, "transcripts_excluded": None}


@pytest.fixture()
def secure_delete_forced_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every store connection runs with ``secure_delete`` off after the store turned it on, so a
    freed page keeps its bytes and only the scrub can remove them."""
    real = Store._open

    def _open(self: Store) -> None:
        real(self)
        self.conn.execute("PRAGMA secure_delete=OFF")

    monkeypatch.setattr(Store, "_open", _open)


def _secret(machine) -> Path:
    """A transcript in the generator's project holding the value, as the card's repro has."""
    path = machine["gen"].with_name("g3.jsonl")
    write_claude_jsonl(
        path, "g3", [("user", "2026-09-16T12:00:00Z", f"the deploy key is {VALUE}, keep it")]
    )
    return path


def _copies(db: str | Path) -> int:
    """The value in a store file and its log with ASCII case folded, as FTS5 keeps a term."""
    files = (Path(db), Path(f"{db}-wal"))
    return sum(f.read_bytes().lower().count(VALUE.lower().encode()) for f in files if f.exists())


@pytest.mark.parametrize("pragma", ["as the store sets it", "forced off"])
def test_exclude_apply_alone_leaves_no_copy_in_the_file_its_index_or_a_later_snapshot(
    machine, capsys, request, pragma
):
    """The card's repro. Before the scrub, the un-index left the lowercased term in the FTS5
    segments, and with ``secure_delete`` off the freed pages kept the turn's bytes, until a
    ``prune --scrub``; a backup taken meanwhile copied them."""
    if pragma == "forced off":
        request.getfixturevalue("secure_delete_forced_off")
    db = machine["db"]
    _secret(machine)
    assert main(["--db", db, "sync"]) == 0
    assert _copies(db) > 0 and check_file(db, VALUE).index_tokens["passage_fts"] == 1
    capsys.readouterr()

    assert main(["--db", db, "exclude", "--add", GLOB, "--apply"]) == 0
    out = capsys.readouterr().out
    snapshot = bk.snapshot(Path(db), machine["dest"])

    left = check_file(db, VALUE)
    assert _copies(db) == 0
    assert (left.turns, left.occurrences_any_case, left.wal_occurrences_any_case or 0) == (0, 0, 0)
    assert left.index_tokens == {"passage_fts": 0, "belief_fts": 0}
    assert _copies(snapshot) == 0
    assert "store file : scrubbed in " in out and "write-ahead log emptied" in out
    assert (
        "left in the search index : nothing: the file was compacted and its log emptied, and no "
        "term of a deleted row is on its index pages"
    ) in out


def test_exclude_apply_reports_the_scrub_names_the_backup_and_skips_it_with_nothing_removed(
    machine, capsys
):
    db, dest = machine["db"], machine["dest"]
    assert main(["--db", db, "sync"]) == 0
    capsys.readouterr()

    assert main(["--db", db, "--json", "exclude", "--add", GLOB, "--apply"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["store_scrubbed"]["indexes"] == list(FTS_TABLES)
    assert out["store_scrubbed"]["wal_truncated"] is True and out["scrub_error"] is None
    assert out["left_in_index"] == {"passage_fts": 0, "belief_fts": 0}
    assert out["backup_dest"] == str(dest)
    progress = [
        line for line in cap.err.splitlines() if line.startswith("scrubbing the store file")
    ]
    assert len(progress) == 3  # both indexes merged, then VACUUM, each announced before it runs
    assert f"the snapshots and mirrored transcripts in {dest}" in cap.err
    assert "`memware scan --backups`" in cap.err
    assert not dest.exists()  # an exclude never writes to a backup destination

    assert main(["--db", db, "exclude", "--add", GLOB, "--apply"]) == 0  # nothing left to un-index
    cap = capsys.readouterr()
    assert "scrubbing" not in cap.err and "store file" not in cap.out


def test_an_exclude_whose_scrub_is_blocked_exits_1_and_says_how_to_finish(
    machine, capsys, monkeypatch
):
    import memware.store as store_module

    monkeypatch.setattr(store_module, "BUSY_TIMEOUT_MS", 100)
    monkeypatch.setattr(store_module, "CHECKPOINT_WAIT_MS", 100)
    db = machine["db"]
    _secret(machine)
    assert main(["--db", db, "sync"]) == 0
    locker = sqlite3.connect(db, isolation_level=None)
    real = Store.scrub

    def scrub_under_a_lock(self, progress=None):
        locker.execute("BEGIN IMMEDIATE")  # another writer takes the lock once the delete commits
        return real(self, progress)

    monkeypatch.setattr(Store, "scrub", scrub_under_a_lock)
    capsys.readouterr()
    try:
        code = main(["--db", db, "exclude", "--add", GLOB, "--apply"])
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    out = capsys.readouterr().out
    assert code == 1
    assert "store file : NOT scrubbed: OperationalError: database is locked" in out
    assert (
        "left in the search index : " in out
        and " of deleted rows on the index pages (passage_fts " in out
    )
    assert "the scrub did not finish" in out and f"memware --db {db} prune --scrub" in out
    assert [_turns_from(db, p) for p in _excluded(machine)] == [0, 0, 0]  # the un-index stands
    assert capture_exclude_patterns() == [GLOB]

    monkeypatch.setattr(Store, "scrub", real)
    assert main(["--db", db, "prune", "--scrub"]) == 0
    assert _copies(db) == 0


def test_an_index_the_scrub_left_holding_deleted_terms_fails_the_exclude(
    machine, capsys, monkeypatch
):
    """The check after the scrub reads the index pages, not the scrub's own report of itself."""
    db = machine["db"]
    assert main(["--db", db, "sync"]) == 0
    monkeypatch.setattr(
        Store, "scrub", lambda self, progress=None: Scrubbed(FTS_TABLES, (), True, 0)
    )
    capsys.readouterr()

    assert main(["--db", db, "exclude", "--add", GLOB, "--apply"]) == 1
    out = capsys.readouterr().out
    assert (
        "left in the search index : " in out
        and " of deleted rows on the index pages (passage_fts " in out
    )
    assert "the search index still holds " in out and f"memware --db {db} prune --scrub" in out


def test_a_reader_mid_read_leaves_the_index_line_not_checked_and_the_exclude_failing(
    machine, capsys, monkeypatch
):
    """Review of #50: with another connection mid-read the log cannot be emptied, and the file keeps
    the pages the scrub rewrote. The check reads the index through the log, so it found nothing and
    the line said so while the file held the value. Now the line says what it could not check."""
    import memware.store as store_module

    monkeypatch.setattr(store_module, "CHECKPOINT_WAIT_MS", 100)
    db = machine["db"]
    _secret(machine)
    assert main(["--db", db, "sync"]) == 0
    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM turn").fetchall()
    capsys.readouterr()
    try:
        code = main(["--db", db, "exclude", "--add", GLOB, "--apply"])
        out = capsys.readouterr().out
        held = _copies(db)
    finally:
        reader.execute("COMMIT")
        reader.close()
    assert code == 1 and held > 0
    assert "write-ahead log NOT emptied" in out
    assert (
        "left in the search index : not checked: another process was reading the store, so the "
        "rewritten pages are still in the write-ahead log"
    ) in out
    assert "left in the search index : nothing" not in out
    assert f"memware --db {db} prune --scrub" in out

    assert main(["--db", db, "prune", "--scrub"]) == 0  # the reader is gone
    assert _copies(db) == 0


def test_the_index_line_never_says_nothing_after_a_scrub_that_did_not_finish():
    from memware.cli import _index_left_line
    from memware.ingest import Pruned
    from memware.ledger import Retraction

    clean = {"passage_fts": 0, "belief_fts": 0}
    r = Pruned({}, 0, Retraction([], [], [], [], []), True, scrub_error="OperationalError: x")
    assert _index_left_line(replace(r, index_left=clean)).startswith(
        "not checked: the scrub did not finish"
    )
    found = replace(r, index_left={"passage_fts": 2, "belief_fts": 0})
    assert _index_left_line(found) == "2 terms of deleted rows on the index pages (passage_fts 2)"
    unread = replace(r, scrub_error=None, scrubbed=Scrubbed(FTS_TABLES, (), True, 0))
    assert _index_left_line(unread) == "not checked: the search index could not be read"
    assert _index_left_line(replace(unread, index_left=clean)).startswith("nothing: ")


def test_an_index_that_cannot_be_read_after_the_scrub_fails_the_exclude(
    machine, capsys, monkeypatch
):
    """A check that could not run is not a clean one."""
    import memware.ingest as ingest

    def unreadable(conn, table, terms=None):
        raise sqlite3.DatabaseError("database disk image is malformed")

    db = machine["db"]
    assert main(["--db", db, "sync"]) == 0
    monkeypatch.setattr(ingest, "deleted_terms", unreadable)
    capsys.readouterr()

    assert main(["--db", db, "exclude", "--add", GLOB, "--apply"]) == 1
    out = capsys.readouterr().out
    assert "store file : scrubbed in " in out
    assert "left in the search index : not checked: the search index could not be read" in out
    assert "the search index could not be read to check it" in out and "prune --scrub" in out


def test_a_path_pattern_that_matches_nothing_is_refused_on_apply_with_the_forms_that_match(
    machine, capsys
):
    """``*/olivia-career/*`` named no directory: Claude Code keeps it as
    ``-Users-me-olivia-career``. The dry run printed ``transcripts : 0`` and ``--apply`` saved it."""
    db, pattern = machine["db"], "*/gen-runs/*"
    assert main(["--db", db, "sync"]) == 0
    before = config_path().read_bytes()
    capsys.readouterr()

    assert main(["--db", db, "exclude", "--add", pattern]) == 0
    dry = capsys.readouterr().out
    assert main(["--db", db, "exclude", "--add", pattern, "--apply"]) == 2
    refused = capsys.readouterr().out
    assert main(["--db", db, "--json", "exclude", "--add", pattern, "--apply"]) == 2
    out = json.loads(capsys.readouterr().out)

    assert config_path().read_bytes() == before  # nothing written
    assert [_turns_from(db, p) for p in _excluded(machine)] == [2, 2, 1]
    broad = "use '*gen-runs*' (4 transcripts on disk, 4 indexed sources)"  # the neighbour too
    narrow = "'*-gen-runs/*' (3 transcripts on disk, 3 indexed sources) covers only"
    assert "the pattern matches no transcript on disk and no indexed source" in dry
    for text in (dry, refused):
        assert broad in text and narrow in text and text.index(broad) < text.index(narrow)
    assert "--apply refuses it" in dry and "--force adds it anyway" in dry
    assert "run again with --apply" not in dry
    assert f"add {pattern} (refused, nothing written)" in refused
    assert "verdict : refused: " in refused
    assert (out["applied"], bool(out["refused"])) == (False, True)
    assert out["suggestions"] == [
        {"pattern": "*gen-runs*", "transcripts": 4, "indexed_sources": 4},
        {"pattern": "*-gen-runs/*", "transcripts": 3, "indexed_sources": 3},
    ]

    assert main(["--db", db, "exclude", "--add", pattern, "--apply", "--force"]) == 0
    assert capture_exclude_patterns() == [pattern]
    assert "added with --force, though it matches nothing" in capsys.readouterr().out


def test_the_form_that_keeps_a_project_out_covers_its_worktrees_and_comes_first(machine, capsys):
    """Review of #50: ``*-name/*`` misses a session started in a worktree or a subdirectory of the
    project, which Claude Code keeps in a directory of its own. Offered first, it made a partial
    privacy exclusion."""
    db, projects = machine["db"], machine["projects"]
    project = projects / "-Users-me-privateproj" / "p1.jsonl"
    worktree = projects / "-Users-me-privateproj--claude-worktrees-feat" / "w1.jsonl"
    for path in (project, worktree):
        path.parent.mkdir()
        write_claude_jsonl(path, path.stem, [("user", "2026-09-16T12:00:00Z", "private plans")])
    assert main(["--db", db, "sync"]) == 0
    capsys.readouterr()

    assert main(["--db", db, "--json", "exclude", "--add", "*/privateproj/*"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert main(["--db", db, "exclude", "--add", "*/privateproj/*"]) == 0
    human = capsys.readouterr().out

    assert [s["pattern"] for s in out["suggestions"]] == ["*privateproj*", "*-privateproj/*"]
    assert [s["transcripts"] for s in out["suggestions"]] == [2, 1]
    assert "To keep the project out, use '*privateproj*' (2 transcripts on disk" in human
    assert "leaves its subdirectories' and worktrees' sessions indexed" in human

    assert main(["--db", db, "exclude", "--add", "*privateproj*", "--apply"]) == 0
    assert (_turns_from(db, project), _turns_from(db, worktree)) == (0, 0)


@pytest.mark.parametrize(
    "pattern",
    ["*/gen-runs*", "*/gen-runs/*/subagents/*", "~/.claude/projects/gen-runs/*"],
)
def test_any_path_pattern_that_matches_nothing_is_refused_with_forms_from_its_last_name(
    machine, capsys, monkeypatch, tmp_path, pattern
):
    """Review of #50: a pattern ending in a wildcard got no suggestion and was saved, and one under
    the transcript source got a form built from the whole path, which matched nothing."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    db = machine["db"]
    assert main(["--db", db, "sync"]) == 0
    before = config_path().read_bytes()
    capsys.readouterr()

    assert main(["--db", db, "--json", "exclude", "--add", pattern, "--apply"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert config_path().read_bytes() == before
    assert out["refused"] and out["suggestions"] == [
        {"pattern": "*gen-runs*", "transcripts": 4, "indexed_sources": 4},
        {"pattern": "*-gen-runs/*", "transcripts": 3, "indexed_sources": 3},
    ]


def test_a_form_that_matches_nothing_is_not_suggested(machine, capsys):
    db = machine["db"]
    assert main(["--db", db, "sync"]) == 0
    capsys.readouterr()

    assert main(["--db", db, "--json", "exclude", "--add", "*/nothere/*", "--apply"]) == 2
    assert json.loads(capsys.readouterr().out)["suggestions"] == []
    assert main(["--db", db, "exclude", "--add", "*/nothere/*"]) == 0
    out = capsys.readouterr().out
    assert "no form of the pattern's last name matches anything either" in out
    assert "'*nothere*'" not in out and "--force adds it anyway" in out


def test_a_dash_encoded_pattern_for_a_project_not_run_yet_needs_force(machine, capsys):
    """Any pattern holding a ``/`` that matches nothing is refused, the form Claude Code uses too;
    --force excludes a project ahead of its first session."""
    args = ["--db", machine["db"], "exclude", "--add", "*/-Users-me-later/*", "--apply"]
    assert main(args) == 2
    assert capture_exclude_patterns() == []
    assert main([*args, "--force"]) == 0
    assert capture_exclude_patterns() == ["*/-Users-me-later/*"]


def test_adding_a_pattern_already_in_the_config_is_a_no_op(machine, capsys):
    """Review of #50: a pattern added with --force was refused when added again."""
    args = ["--db", machine["db"], "exclude", "--add", "*/nothere/*", "--apply"]
    assert main([*args, "--force"]) == 0
    before = config_path().read_bytes()
    capsys.readouterr()

    assert main(args) == 0
    out = capsys.readouterr().out
    assert config_path().read_bytes() == before
    assert "*/nothere/* is already in capture.exclude, so --add changes nothing" in out
    assert "refuse" not in out
    assert main(["--db", machine["db"], "exclude", "--add", "*/nothere/*"]) == 0
    assert "refuse" not in capsys.readouterr().out  # the dry run does not threaten it either


@pytest.mark.parametrize(
    ("pattern", "forms"),
    [
        ("*/olivia-career/*", ["*olivia-career*", "*-olivia-career/*"]),
        ("*/olivia-career", ["*olivia-career*", "*-olivia-career/*"]),
        ("*/Developer/olivia-career/*", ["*olivia-career*", "*-olivia-career/*"]),
        ("*/my.site/*", ["*my-site*", "*-my-site/*"]),
        ("/Users/me/Developer/olivia-career/*", ["*olivia-career*", "*-olivia-career/*"]),
        ("~/work/*", ["*work*", "*-work/*"]),
        ("~/.claude/projects/olivia-career/*", ["*olivia-career*", "*-olivia-career/*"]),
        ("*/olivia-career*", ["*olivia-career*", "*-olivia-career/*"]),
        ("*/olivia-career/*/subagents/*", ["*olivia-career*", "*-olivia-career/*"]),
        ("*/-Users-me-gen-runs/*", ["*Users-me-gen-runs*", "*-Users-me-gen-runs/*"]),
        ("*/g1.jsonl", []),  # a file, and no name before it
        ("*/*/subagents/*", []),  # a directory Claude Code writes itself
        ("*/.claude/projects/*", []),  # the transcript source's own path
    ],
)
def test_the_forms_suggested_for_a_pattern_written_as_a_path(monkeypatch, pattern, forms):
    monkeypatch.setenv("HOME", "/Users/me")
    assert _segment_forms(pattern, {"Users", "me", ".claude", "projects"}) == forms
