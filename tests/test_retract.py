"""Retracting beliefs whose evidence was un-indexed.

A derived belief cites ``memware:session/<id>/turn/<n>``. When that session leaves the index,
by ``memware prune`` or by a sync that honours a marker, the belief loses its evidence and must
stop reaching prompts. These pin the cascade from prune, the one-shot for beliefs that are
already orphaned, the supersession repair, the dry-run default, and that no belief row is ever
deleted. Every store here is synthetic, under the test's own tmp dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memware.cli import main
from memware.derive import source_pointer
from memware.ingest import prune, sync_tree
from memware.ledger import (
    Outcome,
    assert_belief,
    current,
    history,
    orphaned_count,
    plan_retraction,
    pointer_session,
    retract,
    stale_turn_count,
)
from memware.store import Store
from tests.conftest import write_claude_jsonl

MARKER = "[memware-eval]"
WORK, EVAL = "sess-work-1111", "sess-eval-2222"


def _dump(db: str) -> str:
    with Store(db) as s:
        return "\n".join(s.conn.iterdump())


def _beliefs(db: str) -> dict[int, dict[str, object]]:
    with Store(db) as s:
        return {r["id"]: dict(r) for r in s.conn.execute("SELECT * FROM belief")}


def _id(db: str, subject: str, relation: str, value: str) -> int:
    with Store(db) as s:
        row = s.conn.execute(
            "SELECT id FROM belief WHERE subject=? AND relation=? AND value=?",
            (subject, relation, value),
        ).fetchone()
    return int(row["id"])


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture()
def transcripts(tmp_path: Path) -> Path:
    root = tmp_path / "transcripts"
    root.mkdir()
    write_claude_jsonl(
        root / "work.jsonl",
        WORK,
        [
            ("user", "2026-09-01T10:00:00Z", "the staging api listens on 8443 now"),
            ("assistant", "2026-09-01T10:00:05Z", "noted: the staging api port is 8443"),
        ],
    )
    write_claude_jsonl(
        root / "eval.jsonl",
        EVAL,
        [
            ("user", "2026-09-11T09:00:00Z", f"{MARKER} which port does the staging api use"),
            ("assistant", "2026-09-11T09:00:05Z", "the staging api moved to port 9000"),
        ],
    )
    return root


@pytest.fixture()
def db(tmp_path: Path, transcripts: Path) -> str:
    """A real session and an eval run, each with a derived belief; the eval run's belief
    superseded the real one. Two human-stated beliefs share the subject, one naming the eval
    session in its free-text source."""
    path = str(tmp_path / "m.db")
    with Store(path) as s:
        sync_tree(s, transcripts, harness="claude-code")
        turn = {
            r["session"]: int(r["id"])
            for r in s.conn.execute("SELECT session, max(id) AS id FROM turn GROUP BY session")
        }
        for subject, relation, value, when, session in (
            ("staging api", "port", "8443", "2026-09-01T10:00:05Z", WORK),
            ("staging api", "port", "9000", "2026-09-11T09:00:05Z", EVAL),
            ("urlsession background", "task types", "upload only", "2026-09-11T09:00:05Z", EVAL),
        ):
            assert_belief(
                s,
                subject,
                relation,
                value,
                valid_from=when,
                source=source_pointer(session, turn[session]),
            )
        assert_belief(
            s, "staging api", "owner", "platform team", source=f"eric, in {EVAL}", reliability=0.9
        )
        assert_belief(s, "staging api", "region", "us-east-1", reliability=0.9)
    return path


def test_pointer_session_reads_what_derive_writes():
    assert pointer_session(source_pointer(WORK, 7)) == WORK
    assert pointer_session(source_pointer("proj/sub-agent-1", 12)) == "proj/sub-agent-1"
    assert pointer_session(f"eric, in {EVAL}") is None
    assert pointer_session("memware:session/x/turn/") is None
    assert pointer_session(None) is None


def test_prune_dry_run_lists_the_cascade_and_writes_nothing(db, capsys):
    before = _dump(db)
    code, out, err = _run(capsys, "--db", db, "prune", "--containing", MARKER, "--json")
    assert code == 0 and "dry run: nothing written" in err
    r = json.loads(out)
    assert r["applied"] is False
    assert (r["sources_pruned"], r["turns_removed"]) == (1, 2)
    assert r["sessions_emptied"] == [EVAL]
    assert [b["value"] for b in r["retract"]] == ["9000", "upload only"]
    assert [(b["value"], b["was_superseded_by"]) for b in r["reopen"]] == [
        ("8443", _id(db, "staging api", "port", "9000"))
    ]
    assert r["relink"] == []
    assert [b["value"] for b in r["keep"]] == ["platform team"]
    assert _dump(db) == before  # not a turn, not a cursor, not a belief

    code, out, err = _run(capsys, "--db", db, "prune", "--containing", MARKER)
    assert "dry run: nothing written" in err and "dry run" not in out
    assert "beliefs to retract : 2" in out and "predecessors to reopen : 1" in out
    assert "human-stated beliefs kept : 1" in out
    assert "action : reopen" in out and f"source : {source_pointer(EVAL, 2)}" in out
    for line in out.splitlines():
        assert line == "" or " : " in line, line
    assert _dump(db) == before


def test_prune_apply_retracts_and_the_belief_leaves_every_read_path(db, capsys):
    rows_before = _beliefs(db)
    code, out, err = _run(capsys, "--db", db, "prune", "--containing", MARKER, "--apply")
    assert code == 0
    *progress, note = err.splitlines()  # the scrub's steps, then where the text may be left
    assert all(line.startswith("scrubbing the store file: ") for line in progress)
    assert "`memware scan VALUE`" in note
    assert "beliefs retracted : 2" in out and "predecessors reopened : 1" in out

    after = _beliefs(db)
    assert after.keys() == rows_before.keys()  # every row is still there
    gone = [b for b in after.values() if b["status"] == "retracted"]
    assert sorted(b["value"] for b in gone) == ["9000", "upload only"]
    assert all(b["valid_to"] == b["valid_from"] for b in gone)

    _, out, _ = _run(capsys, "--db", db, "beliefs", "--json")
    assert sorted(b["value"] for b in json.loads(out)) == ["8443", "platform team", "us-east-1"]
    _, out, _ = _run(
        capsys, "--db", db, "recall", "staging api port", "urlsession", "--what", "beliefs",
        "--no-touch", "--json",
    )  # fmt: skip
    texts = {h["text"] for h in json.loads(out)}
    assert "staging api port 8443" in texts
    assert not texts & {"staging api port 9000", "urlsession background task types upload only"}
    _, out, _ = _run(capsys, "--db", db, "context", "which port does the staging api use")
    assert "8443" in out and "9000" not in out
    _, out, _ = _run(capsys, "--db", db, "context", "urlsession background task types")
    assert out == ""
    _, out, _ = _run(capsys, "--db", db, "stats", "--json")
    assert json.loads(out)["beliefs_current"] == 3

    _, out, _ = _run(capsys, "--db", db, "beliefs", "staging api", "port", "--json")
    timeline = json.loads(out)
    assert [(b["value"], b["status"]) for b in timeline] == [
        ("8443", "committed"),
        ("9000", "retracted"),
    ]
    assert timeline[0]["valid_to"] is None and timeline[0]["superseded_by"] is None
    assert timeline[1]["retracted_at"]
    assert timeline[1]["retracted_reason"] == (
        f"session {EVAL} is no longer indexed (memware prune --containing '{MARKER}'); "
        f"reopened #{timeline[0]['id']}"
    )


def test_a_human_stated_belief_is_never_retracted(db, capsys):
    human = {
        _id(db, "staging api", "owner", "platform team"),
        _id(db, "staging api", "region", "us-east-1"),
    }
    before = _beliefs(db)
    _run(capsys, "--db", db, "prune", "--containing", MARKER, "--apply")
    with Store(db) as s:  # nor by the one-shot, run after it
        retract(s, reason="test", apply=True)
    after = _beliefs(db)
    assert {i: after[i] for i in human} == {i: before[i] for i in human}


def test_nothing_is_written_without_apply_in_the_library_either(db):
    before = _dump(db)
    with Store(db) as s:
        assert len(prune(s, containing=MARKER).beliefs.retract) == 2
        assert prune(s, turns_containing=MARKER).beliefs.retract == []  # EVAL keeps its reply
    assert _dump(db) == before
    with Store(db) as s:
        s.conn.execute("DELETE FROM turn WHERE session=?", (EVAL,))  # gone, with no cascade
    before = _dump(db)
    with Store(db) as s:
        assert len(retract(s, reason="test").retract) == 2
        assert orphaned_count(s) == 2
    assert _dump(db) == before


def test_the_one_shot_retracts_beliefs_whose_session_is_already_gone(db, tmp_path, capsys):
    # A sync that honours a marker un-indexes the eval run and leaves its beliefs alone.
    home = tmp_path / "memware-home"
    home.mkdir(exist_ok=True)
    (home / "ignore-markers.txt").write_text(MARKER + "\n", encoding="utf-8")
    _run(capsys, "--db", db, "sync", str(tmp_path / "transcripts"))
    assert len(_beliefs(db)) == 5 and not any(
        b["status"] == "retracted" for b in _beliefs(db).values()
    )

    _, out, _ = _run(capsys, "--db", db, "stats")
    assert "beliefs citing an unindexed session : 2" in out
    assert (
        "verdict : 2 beliefs cite a session that is no longer indexed. `memware beliefs retract "
        "--orphaned` lists them; add --apply to retract." in out
    )

    before = _dump(db)
    code, out, err = _run(capsys, "--db", db, "beliefs", "retract", "--orphaned", "--json")
    assert code == 0 and "dry run" in err
    r = json.loads(out)
    assert (r["applied"], r["sessions_emptied"]) == (False, [EVAL])
    assert [b["value"] for b in r["retract"]] == ["9000", "upload only"]
    assert [b["value"] for b in r["reopen"]] == ["8443"]
    assert [b["value"] for b in r["keep"]] == ["platform team"]
    assert _dump(db) == before

    code, out, _ = _run(capsys, "--db", db, "beliefs", "retract", "--orphaned", "--apply")
    assert code == 0 and "beliefs retracted : 2" in out
    _, out, _ = _run(capsys, "--db", db, "stats", "--json")
    assert json.loads(out)["utilization"]["beliefs_orphaned"] == 0
    with Store(db) as s:
        assert [b["value"] for b in current(s, "staging api")] == [
            "platform team",
            "8443",
            "us-east-1",
        ]
        reason = s.conn.execute("SELECT reason FROM retraction ORDER BY belief_id").fetchall()
    assert reason[0][0].startswith(
        f"session {EVAL} is no longer indexed (memware beliefs retract --orphaned); reopened #"
    )

    after = _dump(db)  # a second run finds nothing left to do
    _, out, _ = _run(capsys, "--db", db, "beliefs", "retract", "--orphaned", "--apply")
    assert "beliefs retracted : 0" in out and _dump(db) == after


def test_stats_tells_a_retractable_orphan_from_a_stale_turn_citation(tmp_path, capsys):
    """A session that has no turn left at all is retractable. A session that is still indexed,
    whose cited turn id was renumbered by a re-index, is not — it needs a repair, not a
    retraction, and must never be counted as the same thing (t_0a65da4b)."""
    path = str(tmp_path / "m.db")
    with Store(path) as s:
        s.conn.executemany(
            "INSERT INTO turn(id,session,seq,role,text,source,harness) "
            "VALUES (?,'s1',?,?,?,?,'claude-code')",
            [
                (200, 1, "user", "current turn A", "/p/s1.jsonl"),
                (201, 2, "assistant", "current turn B", "/p/s1.jsonl"),
            ],
        )
        assert_belief(s, "stale", "is", "a dangling citation", source=source_pointer("s1", 50))
        assert_belief(
            s, "gone", "is", "session fully un-indexed", source=source_pointer("s-gone", 1)
        )
        assert orphaned_count(s) == 1
        assert stale_turn_count(s) == 1

    code, out, _ = _run(capsys, "--db", path, "stats")
    assert code == 0
    assert "beliefs citing an unindexed session : 1" in out
    assert "beliefs with a stale turn citation : 1" in out
    verdicts = [line for line in out.splitlines() if line.strip().startswith("verdict")]
    assert sum("no longer indexed" in v for v in verdicts) == 1  # only the retractable one
    assert not any("stale" in v for v in verdicts)  # no call to action: no repair exists yet

    code, out, _ = _run(capsys, "--db", path, "--json", "stats")
    u = json.loads(out)["utilization"]
    assert (u["beliefs_orphaned"], u["beliefs_stale_turn"]) == (1, 1)

    code, out, _ = _run(capsys, "--db", path, "beliefs", "retract", "--orphaned")
    assert "beliefs to retract : 1" in out
    assert "a dangling citation" not in out  # the stale-turn belief is untouched


def test_a_turn_prune_retracts_only_the_sessions_it_empties(db, capsys):
    code, out, _ = _run(
        capsys, "--db", db, "prune", "--turns-starting-with", "the staging api", "--apply", "--json"
    )
    r = json.loads(out)
    # One turn from each session starts with it, and each session keeps its other turn.
    assert (r["turns_removed"], r["sessions_emptied"], r["retract"]) == (2, [], [])
    assert "sources_pruned" not in r

    code, out, _ = _run(
        capsys, "--db", db, "prune", "--turns-containing", MARKER, "--apply", "--json"
    )
    r = json.loads(out)
    assert (r["turns_removed"], r["sessions_emptied"]) == (1, [EVAL])
    assert [b["value"] for b in r["retract"]] == ["9000", "upload only"]


def test_a_retraction_repairs_the_supersession_chain(store):
    def at(day: int) -> str:
        return f"2026-09-{day:02d}T00:00:00Z"

    ptr = {name: source_pointer(name, 1) for name in ("keep-a", "gone-b", "gone-c", "keep-d")}
    store.conn.execute(
        "INSERT INTO turn(session,seq,role,text,source,harness) VALUES "
        "('keep-a',1,'user','a','a.jsonl','x'), ('keep-d',1,'user','d','d.jsonl','x')"
    )
    # x: a -> b -> c, with b and c from gone sessions: a reopens past both.
    for day, value, name in ((1, "a", "keep-a"), (2, "b", "gone-b"), (3, "c", "gone-c")):
        assert_belief(store, "x", "v", value, valid_from=at(day), source=ptr[name])
    # y: a -> b -> d, with only b gone: a is relinked to d and d stays the one current value.
    for day, value, name in ((1, "a", "keep-a"), (2, "b", "gone-b"), (4, "d", "keep-d")):
        assert_belief(store, "y", "v", value, valid_from=at(day), source=ptr[name])

    plan = retract(store, reason="test", apply=True)
    assert plan.sessions == ["gone-b", "gone-c"]
    assert [(p["key"], p["value"]) for p in plan.reopen] == [("x|v", "a")]
    assert [(p["key"], p["value"], p["valid_to"]) for p in plan.relink] == [("y|v", "a", at(4))]
    assert [(b["key"], b["value"]) for b in current(store)] == [("x|v", "a"), ("y|v", "d")]
    y = {b["value"]: b for b in history(store, "y", "v")}
    assert (y["a"]["valid_to"], y["a"]["superseded_by"]) == (at(4), y["d"]["id"])
    assert y["b"]["retracted_reason"].endswith(f"relinked #{y['a']['id']} to #{y['d']['id']}")


def test_a_predecessor_that_is_not_committed_is_never_reopened(store):
    assert_belief(
        store, "x", "v", "a", valid_from="2026-09-01T00:00:00Z", source=source_pointer("s1", 1)
    )
    assert_belief(
        store, "x", "v", "b", valid_from="2026-09-02T00:00:00Z", source=source_pointer("s2", 1)
    )
    retract(store, ["s1"], reason="first", apply=True)  # a is retracted while b supersedes it
    plan = retract(store, ["s2"], reason="second", apply=True)
    assert plan.reopen == [] and plan.relink == []
    assert current(store, "x") == []  # nothing is resurrected from a retracted row


def test_late_evidence_never_lands_on_a_retracted_belief(store):
    assert_belief(
        store, "x", "v", "old", valid_from="2026-09-01T00:00:00Z", source=source_pointer("gone", 1)
    )
    assert_belief(store, "x", "v", "new", valid_from="2026-09-10T00:00:00Z", source="a person")
    retract(store, reason="test", apply=True)
    frozen = [b for b in history(store, "x", "v") if b["status"] == "retracted"]
    assert len(frozen) == 1
    # The same value arrives late, dated between the two: it is new evidence, not a reinforcement
    # of the retracted row, and it closes at the belief that followed it.
    r = assert_belief(store, "x", "v", "old", valid_from="2026-09-05T00:00:00Z", source="a person")
    assert r.outcome is Outcome.HISTORICAL and r.belief_id != frozen[0]["id"]
    rows = {b["id"]: b for b in history(store, "x", "v")}
    assert rows[r.belief_id]["status"] == "committed"
    assert rows[r.belief_id]["valid_to"] == "2026-09-10T00:00:00Z"
    assert rows[frozen[0]["id"]] == frozen[0]  # the retracted row is exactly as it was


def test_a_candidate_and_a_rejected_belief_are_left_alone(store):
    assert_belief(store, "x", "v", "a", reliability=0.9, source="a person")
    r = assert_belief(store, "x", "v", "b", reliability=0.2, source=source_pointer("gone", 1))
    assert r.outcome is Outcome.PENDING_REVIEW
    assert plan_retraction(store).retract == []  # only committed beliefs are retracted


def test_no_belief_row_is_ever_deleted(db, capsys):
    before = _beliefs(db)
    _run(capsys, "--db", db, "prune", "--turns-containing", "the staging api", "--apply")
    _run(capsys, "--db", db, "prune", "--glob", "*eval.jsonl", "--apply")
    _run(capsys, "--db", db, "prune", "--glob", "*", "--apply")
    _run(capsys, "--db", db, "beliefs", "retract", "--orphaned", "--apply")
    after = _beliefs(db)
    assert after.keys() == before.keys()
    assert {
        i: (b["subject"], b["relation"], b["value"], b["source"]) for i, b in after.items()
    } == {i: (b["subject"], b["relation"], b["value"], b["source"]) for i, b in before.items()}
    with Store(db) as s:
        assert s.conn.execute("SELECT count(*) FROM turn").fetchone()[0] == 0
        # every derived belief lost its session; the two a person stated are untouched
        assert sorted(b["value"] for b in current(s)) == ["platform team", "us-east-1"]


def test_usage_errors_exit_2_and_write_nothing(db, capsys):
    before = _dump(db)
    code, _, err = _run(capsys, "--db", db, "prune", "--apply")
    assert code == 2 and "un-index every source" in err
    code, _, err = _run(capsys, "--db", db, "beliefs", "retract", "--apply")
    assert code == 2 and "needs --orphaned" in err
    code, _, err = _run(capsys, "--db", db, "beliefs", "staging api", "--apply")
    assert code == 2 and "belong to `memware beliefs retract`" in err
    assert _dump(db) == before
    # a key whose subject happens to be "retract" is still readable as history
    code, out, _ = _run(capsys, "--db", db, "beliefs", "retract", "anything", "--json")
    assert code == 0 and json.loads(out) == []


def test_plain_output_is_the_records_only(db, capsys):
    _, out, _ = _run(capsys, "--db", db, "prune", "--containing", MARKER, "--plain")
    lines = out.splitlines()
    assert [line.split("\t")[0] for line in lines] == ["retract", "retract", "reopen", "keep"]
