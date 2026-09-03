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
