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


def _age_all(dest):
    """Rename every snapshot one day older — how we advance the clock against the real _now()."""
    import datetime as dt

    for p in sorted(dest.glob("memware-*.db")):
        stamp = bk._stamp(p) - dt.timedelta(days=1)
        p.rename(dest / f"memware-{stamp.strftime('%Y%m%d-%H%M%S')}.db")


def _ages(dest):
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    return sorted((now - bk._stamp(p)).total_seconds() / 86400.0 for p in bk.list_snapshots(dest))


def test_retention_promotes_and_stays_bounded_over_a_daily_loop(tmp_path):
    """The real usage pattern: one new snapshot per day, retention each day. A snapshot must age
    forward through the tiers (not be pruned between them), there is always a ~1-day-old, the pile
    stays small, and nothing lingers far past the largest tier. Files are dated in real time and
    aged by renaming, so this exercises the true `_now()`-based logic."""
    import datetime as dt

    dest = tmp_path / "b"
    dest.mkdir()
    for _day in range(30):
        _age_all(dest)  # yesterday's snapshots become a day older
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")  # a fresh age-0 today
        (dest / f"memware-{stamp}.db").write_bytes(b"x")
        bk.apply_retention(dest, [1, 3, 7, 14])

    ages = _ages(dest)
    assert min(ages) < 1  # always a fresh ~1-day-old
    assert len(ages) <= 6  # bounded, not an unbounded pile
    assert max(ages) <= 15  # nothing lingers far past the largest tier
    assert any(2 <= a <= 9 for a in ages)  # a snapshot promoted into the mid tiers (drift ok)
    assert any(a >= 10 for a in ages)  # and one aged into the oldest tier


def test_retention_prunes_snapshots_older_than_the_largest_tier(tmp_path):
    dest = tmp_path / "b"
    dest.mkdir()
    for age in (0, 2, 6, 13, 20, 40):  # 20 and 40 are past the 14-day tier
        _write_dated(dest, age)
    bk.apply_retention(dest, [1, 3, 7, 14])
    assert all(a <= 15 for a in _ages(dest))


def _write_dated(dest, age_days):
    import datetime as dt

    dest.mkdir(parents=True, exist_ok=True)
    stamp = (dt.datetime.now(dt.UTC) - dt.timedelta(days=age_days)).strftime("%Y%m%d-%H%M%S")
    (dest / f"memware-{stamp}.db").write_bytes(b"x")


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
