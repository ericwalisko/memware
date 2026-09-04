import io
import json
import sys

from memware.cli import main
from tests.conftest import write_claude_jsonl


def test_cli_end_to_end(tmp_path, capsys):
    db = str(tmp_path / "c.db")
    p = tmp_path / "s.jsonl"
    write_claude_jsonl(
        p, "s", [("assistant", "2026-08-20T00:00:00Z", "the ingest job runs nightly at three")]
    )
    assert main(["--db", db, "sync", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 1
    main(["--db", db, "assert", "ingest job", "runs at", "03:00", "--json"])
    capsys.readouterr()
    assert main(["--db", db, "recall", "ingest nightly", "--json"]) == 0
    assert {h["kind"] for h in json.loads(capsys.readouterr().out)} == {"turn", "belief"}
    assert main(["--db", db, "context", "when does the ingest job run"]) == 0
    assert "03:00" in capsys.readouterr().out
    assert main(["--db", db, "stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["beliefs_current"] == 1


def test_context_from_hook_emits_hook_json(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "h.db")
    main(["--db", db, "assert", "api", "port", "8443"])
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "which api port"})))
    main(["--db", db, "context", "--from-hook"])
    out = json.loads(capsys.readouterr().out)
    assert "8443" in out["hookSpecificOutput"]["additionalContext"]


def test_backfill_indexes_existing_transcripts(tmp_path, capsys):
    import json

    from tests.conftest import write_claude_jsonl

    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    write_claude_jsonl(
        root / "s.jsonl",
        "s",
        [
            (
                "assistant",
                "2026-08-20T00:00:00Z",
                "the deploy script runs blue-green rollouts nightly",
            )
        ],
    )
    db = str(tmp_path / "b.db")
    assert main(["--db", db, "backfill", str(tmp_path / "projects"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["turns_added"] == 1 and out["sessions"] == 1
    # idempotent
    assert main(["--db", db, "backfill", str(tmp_path / "projects"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["turns_added"] == 0


def test_setup_yes_backfills_and_makes_first_backup(tmp_path, capsys, monkeypatch):
    """A fresh-install walkthrough end to end: --yes indexes the sessions already on disk and
    takes a first backup, and it marks setup done so the discovery hint stops."""
    from memware import __version__
    from memware import backup as bk
    from memware.config import get_dotted, load_config

    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    projects = tmp_path / "projects"
    (projects / "p").mkdir(parents=True)
    write_claude_jsonl(
        projects / "p" / "s.jsonl",
        "s",
        [("assistant", "2026-08-20T00:00:00Z", "the nightly job compacts the write-ahead log")],
    )
    dest = tmp_path / "dropbox" / "memware"
    db = str(tmp_path / "m.db")
    for k, v in (("backup.transcript_src", str(projects)), ("backup.dest", str(dest))):
        main(["--db", db, "config", k, str(v)])
    capsys.readouterr()

    assert main(["--db", db, "setup", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "indexed 1 turns" in out

    assert main(["--db", db, "stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["turns"] == 1  # backfill happened
    assert get_dotted(load_config(), "setup.completed_version") == __version__
    assert bk.list_snapshots(dest)  # a first snapshot was taken
    assert (dest / "transcripts" / "p" / "s.jsonl").exists()  # transcripts mirrored


def test_setup_hint_shows_until_backups_configured(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    db = str(tmp_path / "m.db")

    main(["--db", db, "stats"])  # never set up -> the tip appears on stderr
    assert "memware setup" in capsys.readouterr().err

    main(["--db", db, "stats", "--json"])  # machine-readable callers never see it
    assert "memware setup" not in capsys.readouterr().err

    main(["--db", db, "config", "backup.dest", str(tmp_path / "bk")])
    capsys.readouterr()
    main(["--db", db, "stats"])  # once a destination exists the tip is gone
    assert "memware setup" not in capsys.readouterr().err


def test_bare_sync_catches_up_configured_transcript_src(tmp_path, capsys, monkeypatch):
    """`memware sync` with no path indexes the configured transcript source — what the
    SessionStart hook runs to catch up sessions whose SessionEnd never fired (e.g. a worktree
    force-killed by Orca). A path or --from-hook still targets exactly what's given."""
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    projects = tmp_path / "projects"
    (projects / "p").mkdir(parents=True)
    write_claude_jsonl(
        projects / "p" / "s.jsonl",
        "s",
        [("assistant", "2026-08-25T00:00:00Z", "the indexer catches up on the next session start")],
    )
    db = str(tmp_path / "m.db")
    main(["--db", db, "config", "backup.transcript_src", str(projects)])
    capsys.readouterr()
    assert main(["--db", db, "sync", "--json"]) == 0  # no path -> configured source
    assert json.loads(capsys.readouterr().out)["added"] == 1
