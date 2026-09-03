import json

from memware import backup as bk
from memware.cli import main
from memware.store import Store
from tests.conftest import write_claude_jsonl


def _seed(db, n_turns=3):
    with Store(db) as s:
        for i in range(n_turns):
            s.conn.execute(
                "INSERT INTO turn(session,seq,ts,role,text,source,harness) "
                "VALUES ('s',?,'t','assistant',?, 'x','claude-code')",
                (i, f"turn number {i} about the caching layer and its retry policy"),
            )
    return db


def test_snapshot_is_consistent_and_restore_round_trips(tmp_path):
    db = _seed(tmp_path / "m.db", 5)
    dest = tmp_path / "backups"
    snap = bk.snapshot(db, dest)
    assert snap.exists() and snap.parent == dest
    with Store(db) as s:
        s.conn.execute("DELETE FROM turn")  # simulate damage/wipe
        assert s.stats()["turns"] == 0
    prev = bk.restore(snap, db)
    assert prev.exists()  # the damaged store was saved aside, not lost
    with Store(db) as s:
        assert s.stats()["turns"] == 5


def test_tiered_retention_keeps_one_per_bucket(tmp_path):
    import datetime as dt

    dest = tmp_path / "b"
    dest.mkdir()
    now = dt.datetime.now(dt.UTC)
    # one snapshot per day for 20 days
    for age in range(20):
        stamp = (now - dt.timedelta(days=age)).strftime("%Y%m%d-%H%M%S")
        (dest / f"memware-{stamp}.db").write_bytes(b"x")
    bk.apply_retention(dest, [1, 3, 7, 14])
    kept = bk.list_snapshots(dest)
    # newest + one each at >=1,>=3,>=7,>=14 days = at most 5, far fewer than 20
    assert 4 <= len(kept) <= 5


def test_backup_cli_uses_config_dest_and_mirrors_transcripts(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    db = _seed(tmp_path / "m.db", 2)
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    write_claude_jsonl(
        proj / "s.jsonl", "s", [("assistant", "2026-08-01T00:00:00Z", "a real prior session line")]
    )
    dest = tmp_path / "dropbox" / "memware"
    main(["--db", str(db), "config", "backup.dest", str(dest)])
    capsys.readouterr()
    main(["--db", str(db), "config", "backup.transcript_src", str(tmp_path / "projects")])
    capsys.readouterr()
    assert main(["--db", str(db), "backup", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["snapshot"].endswith(".db") and out["transcripts_mirrored"] == 1
    assert (dest / "transcripts" / "p" / "s.jsonl").exists()


def test_nuke_requires_exact_phrase(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    db = _seed(tmp_path / "m.db", 1)
    dest = tmp_path / "bk"
    main(["--db", str(db), "config", "backup.dest", str(dest)])
    capsys.readouterr()
    bk.snapshot(db, dest)
    # wrong phrase: nothing deleted
    assert main(["--db", str(db), "nuke", "--confirm", "delete"]) == 1
    assert (tmp_path / "m.db").exists() and bk.list_snapshots(dest)
    # exact phrase: store and snapshots gone
    assert main(["--db", str(db), "nuke", "--confirm", "DELETE ALL MEMWARE DATA"]) == 0
    assert not (tmp_path / "m.db").exists() and not bk.list_snapshots(dest)


def test_backfill_warns_when_backup_is_larger(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    db = _seed(tmp_path / "m.db", 400)
    dest = tmp_path / "bk"
    main(["--db", str(db), "config", "backup.dest", str(dest)])
    capsys.readouterr()
    bk.snapshot(db, dest)  # backup with 400 turns
    with Store(db) as s:
        s.conn.execute("DELETE FROM turn")  # user wiped
    proj = tmp_path / "projects"
    proj.mkdir()
    write_claude_jsonl(
        proj / "s.jsonl",
        "s",
        [("assistant", "2026-08-01T00:00:00Z", "only a little is left on disk")],
    )
    main(["--db", str(db), "backfill", str(proj)])
    err = capsys.readouterr().err
    assert "restore" in err and "400" in err


def test_if_stale_throttles_and_no_dest_is_silent_noop(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    db = _seed(tmp_path / "m.db", 2)
    # no dest configured yet: --if-stale is a silent no-op (safe from a hook)
    assert main(["--db", str(db), "backup", "--if-stale", "20", "--quiet"]) == 0
    assert capsys.readouterr().out == ""
    dest = tmp_path / "bk"
    main(["--db", str(db), "config", "backup.dest", str(dest)])
    capsys.readouterr()
    # first stale-check has no snapshot yet -> it backs up
    assert main(["--db", str(db), "backup", "--if-stale", "20", "--no-transcripts", "--json"]) == 0
    n1 = len(bk.list_snapshots(dest))
    assert n1 == 1
    # immediate second call is within the window -> skipped, no new snapshot
    assert main(["--db", str(db), "backup", "--if-stale", "20", "--no-transcripts", "--json"]) == 0
    assert len(bk.list_snapshots(dest)) == n1
