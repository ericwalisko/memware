"""``capture.exclude``: path globs in the config that no sync indexes and no backup mirrors.

``MEMWARE_NO_CAPTURE`` reaches only the processes a run starts, and three harnesses forgot it in
two days. A marker needs its text inside the transcript. A pattern lives in the machine's config,
so it excludes a generator by where its sessions run, whatever the generator's author forgot.
Every test here runs on a synthetic transcript tree and a scratch ``MEMWARE_HOME``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memware import backup as bk
from memware.cli import main
from memware.config import config_path, memware_home
from memware.ingest import (
    capture_exclude_patterns,
    is_excluded,
    matches_exclude,
    record_no_capture,
    sync_file,
)
from memware.store import Store
from tests.conftest import write_claude_jsonl

GLOB = "*/-Users-me-gen-runs/*"


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
    assert [_turns_from(db, p) for p in _excluded(machine)] == [0, 0, 0]
    assert _turns_from(db, machine["wiki"]) == 1

    assert main(["--db", db, "exclude", "--add", GLOB, "--apply"]) == 0  # idempotent
    assert capture_exclude_patterns() == [GLOB]


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
    assert "`*/name/*` names a directory" in capsys.readouterr().out


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
