"""Provenance: which sessions derive reads, and where the store came from.

Claude Code writes ``entrypoint`` on every conversation record: ``cli`` for an interactive
session, ``sdk-cli`` for ``claude -p``. Sync keeps it per turn, ``derive.sources`` decides
whether derive reads headless turns (default: no), and ``memware stats`` breaks the store down
by it. A turn with no entrypoint is read as interactive, so a transcript written before Claude
Code recorded the field, or by a parser that cannot know, is never dropped without a word.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memware import derive as md
from memware.cli import _provenance_lines, main
from memware.ingest import sync_file, sync_tree
from memware.ingest.claude_code import transcript_entrypoint
from memware.ledger import assert_belief
from memware.passage import index_turn
from memware.store import SCHEMA, SCHEMA_VERSION, Store, project_dir
from tests.conftest import write_claude_jsonl
from tests.test_derive import forbid_provider, stub

ROOT = Path(__file__).resolve().parents[1]

CLI_FACT = "The deploy target is now fly.io in the iad region."
SUBAGENT_FACT = "The subagent confirmed the port is now 8443 on staging."
OLD_FACT = "The backup destination lives in the Dropbox memware folder."
LANE_FACTS = (
    "The judge rubric is pinned at version 7 for the eval.",
    "The fixture corpus lives in the gen-runs scratch folder.",
)
FILLER = "Understood, I will keep that in mind."


@pytest.fixture()
def projects(tmp_path: Path) -> Path:
    """A Claude Code projects tree: one interactive session with a subagent and one session
    from before the field in ``-Users-me-work``, three ``claude -p`` sessions in
    ``-Users-me-gen``. Eight turns: four interactive or unlabelled, four headless."""
    root = tmp_path / "projects"
    work, gen = root / "-Users-me-work", root / "-Users-me-gen"
    (work / "s-cli" / "subagents").mkdir(parents=True)
    gen.mkdir(parents=True)
    ts = "2026-09-16T10:00:00Z"
    write_claude_jsonl(
        work / "s-cli.jsonl",
        "s-cli",
        [("user", ts, CLI_FACT), ("assistant", ts, FILLER)],
        entrypoint="cli",
    )
    write_claude_jsonl(
        work / "s-cli" / "subagents" / "agent-a.jsonl",
        "s-cli",
        [("assistant", ts, SUBAGENT_FACT)],
        entrypoint="cli",
    )
    write_claude_jsonl(work / "s-old.jsonl", "s-old", [("user", ts, OLD_FACT)])
    write_claude_jsonl(
        gen / "s-lane-1.jsonl",
        "s-lane-1",
        [("user", ts, LANE_FACTS[0]), ("assistant", ts, FILLER)],
        entrypoint="sdk-cli",
    )
    write_claude_jsonl(
        gen / "s-lane-2.jsonl", "s-lane-2", [("user", ts, LANE_FACTS[1])], entrypoint="sdk-cli"
    )
    write_claude_jsonl(
        gen / "s-lane-3.jsonl",
        "s-lane-3",
        [("user", ts, "Please run the nightly examiner across the queue.")],
        entrypoint="sdk-cli",
    )
    return root


@pytest.fixture()
def db(tmp_path: Path, projects: Path) -> str:
    path = tmp_path / "synthetic.db"
    with Store(path) as s:
        added = sync_tree(s, projects, harness="claude-code")
    assert sum(added.values()) == 8
    return str(path)


def _turn_id(db: str, text: str) -> int:
    with Store(db) as s:
        return int(s.conn.execute("SELECT id FROM turn WHERE text=?", (text,)).fetchone()[0])


# ── sync records it ────────────────────────────────────────────────────


def test_sync_keeps_each_records_entrypoint_and_none_where_there_is_none(db):
    with Store(db) as s:
        got = {
            r["text"]: r["entrypoint"] for r in s.conn.execute("SELECT text, entrypoint FROM turn")
        }
    assert got[CLI_FACT] == "cli" and got[SUBAGENT_FACT] == "cli"
    assert got[LANE_FACTS[0]] == got[LANE_FACTS[1]] == "sdk-cli"
    assert got[OLD_FACT] is None


def test_a_session_that_changes_entrypoint_keeps_it_per_turn(store, tmp_path):
    """Resuming a `claude -p` session interactively writes both values into one file."""
    p = tmp_path / "mixed.jsonl"
    write_claude_jsonl(
        p, "m", [("user", "2026-09-16T10:00:00Z", LANE_FACTS[0])], entrypoint="sdk-cli"
    )
    head = p.read_text()
    write_claude_jsonl(p, "m", [("user", "2026-09-16T11:00:00Z", CLI_FACT)], entrypoint="cli")
    p.write_text(head + p.read_text())
    assert sync_file(store, p, harness="claude-code") == 2
    rows = store.conn.execute("SELECT entrypoint FROM turn ORDER BY seq").fetchall()
    assert [r[0] for r in rows] == ["sdk-cli", "cli"]
    assert transcript_entrypoint(p) == "sdk-cli"  # the first conversation record decides


def test_transcript_entrypoint_is_none_without_the_field_or_the_file(tmp_path):
    old = tmp_path / "old.jsonl"
    write_claude_jsonl(old, "o", [("user", "2026-07-01T00:00:00Z", OLD_FACT)])
    assert transcript_entrypoint(old) is None
    assert transcript_entrypoint(tmp_path / "gone.jsonl") is None


def test_the_generic_parser_cannot_know_and_leaves_it_null(store, tmp_path):
    p = tmp_path / "hermes.jsonl"
    p.write_text(json.dumps({"role": "user", "content": OLD_FACT, "session": "h"}) + "\n")
    assert sync_file(store, p, harness="generic") == 1
    assert store.conn.execute("SELECT entrypoint FROM turn").fetchone()[0] is None


# ── derive reads interactive sessions by default ───────────────────────


def _plan(db: str, tmp_path: Path, capsys) -> str:
    assert main(["--db", db, "derive", "--state", str(tmp_path / "state.json"), "--plan"]) == 0
    return capsys.readouterr().out


def test_plan_reads_cli_and_unlabelled_turns_by_default_and_all_after_the_opt_in(
    db, tmp_path, monkeypatch, capsys
):
    forbid_provider(monkeypatch)
    out = _plan(db, tmp_path, capsys)
    assert "sources     : interactive (derive.sources)\n" in out
    assert "turns       : 4 under interactive, 8 under all (new since the watermark)\n" in out
    assert "sessions    : 2 with new turns\n" in out
    for fact in (CLI_FACT, SUBAGENT_FACT, OLD_FACT):
        assert fact in out
    for fact in LANE_FACTS:
        assert fact not in out

    main(["--db", db, "config", "derive.sources", "all"])
    capsys.readouterr()
    out = _plan(db, tmp_path, capsys)
    assert "sources     : all (derive.sources)\n" in out
    assert "turns       : 4 under interactive, 8 under all (new since the watermark)\n" in out
    assert "sessions    : 5 with new turns\n" in out
    for fact in (CLI_FACT, SUBAGENT_FACT, OLD_FACT, *LANE_FACTS):
        assert fact in out


def test_a_run_sends_only_interactive_excerpts_by_default_and_all_under_all(
    db, tmp_path, monkeypatch, capsys
):
    state = str(tmp_path / "state.json")
    prov = stub(monkeypatch, [[]], chunk=24)
    assert main(["--db", db, "derive", "--state", state]) == 0
    sent = prov.prompts[0]
    assert CLI_FACT in sent and SUBAGENT_FACT in sent and OLD_FACT in sent
    assert not any(f in sent for f in LANE_FACTS)
    assert "sources     : interactive (derive.sources)" in capsys.readouterr().out

    main(["--db", db, "config", "derive.sources", "all"])
    prov = stub(monkeypatch, [[]], chunk=24)
    assert main(["--db", db, "derive", "--state", state]) == 0
    assert all(f in prov.prompts[0] for f in (CLI_FACT, SUBAGENT_FACT, OLD_FACT, *LANE_FACTS))


def test_the_watermark_passes_skipped_turns_and_since_reads_them_again(
    db, tmp_path, monkeypatch, capsys
):
    """Opting in later changes what the next run reads, not what earlier runs read."""
    state = tmp_path / "state.json"
    stub(monkeypatch, [[]], chunk=24)
    assert main(["--db", db, "derive", "--state", str(state), "--apply"]) == 0
    with Store(db) as s:
        head = int(s.conn.execute("SELECT max(id) FROM turn").fetchone()[0])
    assert md.load_state(state)["watermark"] == head
    main(["--db", db, "config", "derive.sources", "all"])
    capsys.readouterr()
    assert "turns       : 0 under interactive, 0 under all" in _plan(db, tmp_path, capsys)
    argv = ["--db", db, "derive", "--state", str(state), "--plan", "--since", "0"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert all(f in out for f in LANE_FACTS)


def test_an_unknown_setting_refuses_before_any_provider_exists(db, tmp_path, monkeypatch, capsys):
    main(["--db", db, "config", "derive.sources", "headless"])
    forbid_provider(monkeypatch)
    for extra in (["--plan"], [], ["--apply"]):
        argv = ["--db", db, "derive", "--state", str(tmp_path / "state.json"), *extra]
        assert main(argv) == md.EXIT_CONFIG
        assert (
            "derive.sources is 'headless'; expected interactive or all" in capsys.readouterr().err
        )
    assert not (tmp_path / "state.json").exists()


def test_only_the_non_interactive_entrypoints_are_skipped(store):
    """An entrypoint the list does not name (an IDE extension, the desktop app) is read."""
    rows = [
        ("vscode", "claude-vscode"),
        ("desktop", "claude-desktop"),
        ("none", None),
        *[(ep, ep) for ep in md.HEADLESS_ENTRYPOINTS],
    ]
    store.conn.executemany(
        "INSERT INTO turn(session,seq,role,text,source,harness,entrypoint) "
        "VALUES (?,1,'user','a fact that is now true',?,'claude-code',?)",
        [(session, f"{session}.jsonl", ep) for session, ep in rows],
    )
    read = md.sessions_with_new_turns(store.conn, 0)
    assert sorted(read) == ["desktop", "none", "vscode"]
    assert len(md.sessions_with_new_turns(store.conn, 0, sources="all")) == len(rows)
    assert md.turns_since(store.conn, 0, "interactive") == 3


# ── stats shows the breakdown ──────────────────────────────────────────


def test_stats_breaks_the_store_down_by_entrypoint_and_project(db, projects, capsys):
    with Store(db) as s:
        assert_belief(
            s,
            "deploy target",
            "host",
            "fly.io",
            source=md.source_pointer("s-cli", _turn_id(db, CLI_FACT)),
        )
        assert_belief(
            s,
            "judge rubric",
            "pinned version",
            "7",
            source=md.source_pointer("s-lane-1", _turn_id(db, LANE_FACTS[0])),
        )
        assert_belief(s, "editor", "is", "helix")  # stated by a person: no turn
        # a real turn id under the wrong, never-indexed session: retractable, not stale
        assert_belief(
            s, "stale", "is", "gone", source=md.source_pointer("s-gone", _turn_id(db, CLI_FACT))
        )

    assert main(["--db", db, "stats", "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    work, gen = (
        str((projects / "-Users-me-work").resolve()),
        str((projects / "-Users-me-gen").resolve()),
    )
    assert r["provenance"] == {
        "by_entrypoint": [
            {"entrypoint": "sdk-cli", "sessions": 3, "turns": 4, "beliefs": 1},
            {"entrypoint": "cli", "sessions": 1, "turns": 3, "beliefs": 1},
            {"entrypoint": None, "sessions": 1, "turns": 1, "beliefs": 0},
        ],
        "top_projects": [
            {"directory": gen, "sessions": 3, "share": 0.6},
            {"directory": work, "sessions": 2, "share": 0.4},  # the subagent is s-cli's
        ],
    }
    assert r["derive"]["sources"] == "interactive" and r["derive"]["turns_pending"] == 4
    # "editor is helix" cites nothing; "stale is gone" cites a session never indexed at all —
    # that is the retractable kind, not a stale turn citation inside a session still indexed.
    assert (r["utilization"]["beliefs_orphaned"], r["utilization"]["beliefs_stale_turn"]) == (1, 0)

    assert main(["--db", db, "stats"]) == 0
    out = capsys.readouterr().out
    assert "entrypoint sdk-cli : 3 sessions, 4 turns, 1 belief\n" in out
    assert "entrypoint cli : 1 session, 3 turns, 1 belief\n" in out
    assert (
        "no entrypoint : 1 session, 1 turn, 0 beliefs (derive reads these as interactive)\n" in out
    )
    assert "beliefs citing an unindexed session : 1\n" in out
    assert "beliefs with a stale turn citation : 0\n" in out
    assert f"project directory : 3 sessions (60.0%) {gen}\n" in out
    assert f"project directory : 2 sessions (40.0%) {work}\n" in out
    assert "derive sources : interactive\n" in out
    for line in out.splitlines():
        assert line == "" or " : " in line, line

    main(["--db", db, "config", "derive.sources", "all"])
    capsys.readouterr()
    assert main(["--db", db, "stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["derive"]["turns_pending"] == 8


def test_stats_lists_at_most_five_project_directories_and_shortens_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    report = {
        "by_entrypoint": [],
        "top_projects": [
            {"directory": f"{home}/.claude/projects/-a", "sessions": 1, "share": 1.0},
            {"directory": "/elsewhere/sessions", "sessions": 1, "share": 1.0},
        ],
    }
    assert _provenance_lines(report) == [
        ("project directory", "1 session (100.0%) ~/.claude/projects/-a"),
        ("project directory", "1 session (100.0%) /elsewhere/sessions"),
    ]
    with Store(tmp_path / "many.db") as s:
        s.conn.executemany(
            "INSERT INTO turn(session,seq,role,text,source,harness) "
            "VALUES (?,1,'user','text',?,'claude-code')",
            [(f"s{i}", f"/p/dir{i}/s{i}.jsonl") for i in range(7)],
        )
        assert len(s.provenance()["top_projects"]) == 5
        assert s.provenance()["by_entrypoint"] == [
            {"entrypoint": None, "sessions": 7, "turns": 7, "beliefs": 0}
        ]


def test_project_dir_lifts_a_subagent_transcript_to_its_project():
    assert project_dir("/p/-Users-me/abc.jsonl") == "/p/-Users-me"
    assert project_dir("/p/-Users-me/abc/subagents/agent-1.jsonl") == "/p/-Users-me"


# ── an existing store gains the column ─────────────────────────────────

V2_TURN = """
CREATE TABLE turn (
  id        INTEGER PRIMARY KEY,
  session   TEXT NOT NULL,
  seq       INTEGER NOT NULL,
  ts        TEXT,
  role      TEXT NOT NULL,
  text      TEXT NOT NULL,
  source    TEXT NOT NULL,
  harness   TEXT NOT NULL,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_used TEXT,
  UNIQUE(source, seq)
);
"""


def test_a_version_2_store_gains_the_column_keeps_every_row_and_backfills(tmp_path, projects):
    """The turn table as version 2 wrote it, indexed from transcripts that are still on disk,
    one that is gone, and a generic source. Opening it migrates once; opening again does
    nothing."""
    cli = str((projects / "-Users-me-work" / "s-cli.jsonl").resolve())
    lane = str((projects / "-Users-me-gen" / "s-lane-1.jsonl").resolve())
    old = str((projects / "-Users-me-work" / "s-old.jsonl").resolve())
    gone = str(tmp_path / "deleted" / "s-gone.jsonl")
    rows = [
        (1, "s-cli", 1, "user", CLI_FACT, cli, "claude-code"),
        (2, "s-cli", 2, "assistant", FILLER, cli, "claude-code"),
        (3, "s-lane-1", 1, "user", LANE_FACTS[0], lane, "claude-code"),
        (4, "s-old", 1, "user", OLD_FACT, old, "claude-code"),
        (5, "s-gone", 1, "user", "A session whose transcript was cleaned up.", gone, "claude-code"),
        (6, "h", 1, "user", "A Hermes turn that is now in the store.", "/h/x.jsonl", "generic"),
    ]
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(V2_TURN + SCHEMA)  # the other tables and triggers are unchanged
    conn.executemany(
        "INSERT INTO turn(id,session,seq,role,text,source,harness) VALUES (?,?,?,?,?,?,?)", rows
    )
    for turn_id, *_, text, _source, _harness in rows:
        index_turn(conn, turn_id, text)
    conn.execute("UPDATE turn SET use_count=2, last_used='2026-09-01T00:00:00Z' WHERE id=1")
    conn.execute("PRAGMA user_version = 2")
    passages = conn.execute("SELECT count(*) FROM passage").fetchone()[0]
    conn.close()

    def snapshot(s: Store) -> list[tuple[object, ...]]:
        return [
            tuple(r)
            for r in s.conn.execute(
                "SELECT id, session, seq, role, text, source, harness, use_count, entrypoint "
                "FROM turn ORDER BY id"
            )
        ]

    with Store(path) as s:
        assert int(s.conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION == 3
        after = snapshot(s)
        assert s.conn.execute("SELECT count(*) FROM passage").fetchone()[0] == passages
    assert [r[:8] for r in after] == [(*r[:6], r[6], 2 if r[0] == 1 else 0) for r in rows]
    assert {r[0]: r[8] for r in after} == {
        1: "cli",
        2: "cli",
        3: "sdk-cli",
        4: None,  # written before the field: read as interactive
        5: None,  # the transcript is gone
        6: None,  # a generic source is never read for it
    }

    with Store(path) as s:  # idempotent: a second open neither alters nor rewrites
        assert snapshot(s) == after
        assert s.backfill_entrypoints() == 0
        columns = [r["name"] for r in s.conn.execute("PRAGMA table_info(turn)")]
    assert columns.count("entrypoint") == 1


def test_a_new_store_puts_the_column_where_a_migrated_one_has_it(tmp_path):
    """Both end with it, so a row read by position lines up whichever way the store was made."""
    with Store(tmp_path / "new.db") as s:
        columns = [r["name"] for r in s.conn.execute("PRAGMA table_info(turn)")]
    assert columns[-1] == "entrypoint"


# ── the docs say it ────────────────────────────────────────────────────


@pytest.mark.parametrize("doc", ["README.md", "docs/scheduling.md", "docs/keeping-memory-clean.md"])
def test_the_docs_name_the_default_and_the_opt_in(doc):
    flat = " ".join((ROOT / doc).read_text().split())
    assert "memware config derive.sources all" in flat, doc
    assert "interactive" in flat, doc
