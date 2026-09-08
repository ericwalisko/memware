import json
import os

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


def test_mirror_never_opens_the_target_for_writing(tmp_path, monkeypatch):
    """A synced destination (Dropbox) evicts uploaded files to dataless placeholders, and
    opening one of those for write makes the sync engine materialise it first — which
    failed with EDEADLK on five nightly runs in a row (2026-09-04 → 09-08). So the mirror
    writes beside the target and renames over it; the target itself is never opened."""
    import builtins
    import errno
    from pathlib import Path

    src = tmp_path / "projects" / "p"
    src.mkdir(parents=True)
    (src / "s.jsonl").write_text("new\n")
    dest = tmp_path / "dropbox"
    target = dest / "transcripts" / "p" / "s.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("old")
    os.utime(target, (0, 0))  # stale: older and smaller than the source
    real_open = builtins.open

    def refuse_writes_to_target(file, mode="r", *a, **k):
        if Path(str(file)) == target and any(c in mode for c in "wa+"):
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", refuse_writes_to_target)
    res = bk.mirror_transcripts(tmp_path / "projects", dest)
    assert (res.copied, res.skipped) == (1, [])
    assert target.read_text() == "new\n"
    assert not list(target.parent.glob(".*.mw-tmp"))  # no temp litter beside it


def test_mirror_skips_an_unwritable_target_and_keeps_going(tmp_path, monkeypatch):
    """One file that cannot be written is reported, not raised: it used to take the whole
    run — and `backup`'s exit code, and the cron reading it — down with it."""
    import errno
    from pathlib import Path

    src = tmp_path / "projects"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir()
    (src / "a" / "x.jsonl").write_text("x")
    (src / "b" / "y.jsonl").write_text("y")
    dest = tmp_path / "dropbox"
    real_replace = os.replace

    def deadlock_on_x(s, d):
        if Path(d).name == "x.jsonl":
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return real_replace(s, d)

    monkeypatch.setattr(os, "replace", deadlock_on_x)
    res = bk.mirror_transcripts(src, dest)
    assert res.copied == 1
    assert [t.name for t, _ in res.skipped] == ["x.jsonl"]
    assert "errno 11" in res.skipped[0][1]
    assert (dest / "transcripts" / "b" / "y.jsonl").read_text() == "y"
    assert not list((dest / "transcripts" / "a").glob(".*"))  # the temp file was cleaned up


def test_backup_cli_reports_skipped_transcripts_and_still_exits_zero(tmp_path, capsys, monkeypatch):
    """The snapshot is the thing that must not be lost; a mirror skip is retried tomorrow."""
    import errno
    from pathlib import Path

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
    real_replace = os.replace

    def deadlock_on_transcripts(s, d):
        if Path(d).suffix == ".jsonl":
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return real_replace(s, d)

    monkeypatch.setattr(os, "replace", deadlock_on_transcripts)
    assert main(["--db", str(db), "backup", "--json"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["snapshot"].endswith(".db")
    assert (out["transcripts_mirrored"], out["transcripts_skipped"]) == (0, 1)
    assert "transcript not mirrored" in cap.err and "errno 11" in cap.err
