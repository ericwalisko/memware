"""``memware stats`` — counts that say whether memory is doing anything.

A derive run that found nothing and a derive run that never executed used to print the same
six numbers. These pin the derive and utilization sections, the plain-language verdicts for an
inert store, and that prompt-hook injection no longer counts as a recall.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from memware import derive as md
from memware.cli import main
from memware.ledger import assert_belief
from memware.store import Store, age_hours, now_iso

SIX = ("turns", "passages", "sessions", "beliefs_current", "beliefs_total", "reviews_open")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def db(tmp_path) -> str:
    """Three conversation turns and one tool turn in two sessions; no beliefs, no derive state."""
    path = tmp_path / "m.db"
    with Store(path) as s:
        s.conn.executemany(
            "INSERT INTO turn(id,session,seq,ts,role,text,source,harness) "
            "VALUES (?,?,?,?,?,?,?,'claude-code')",
            [
                (1, "a", 1, "2026-08-01T10:00:00Z", "user", "the api listens on 8443", "a.jsonl"),
                (2, "a", 2, "2026-08-01T10:01:00Z", "assistant", "noted, port 8443", "a.jsonl"),
                (3, "b", 1, "2026-09-01T09:00:00Z", "user", "deploys go blue-green", "b.jsonl"),
                (4, "b", 2, "2026-09-01T09:01:00Z", "tool", "tool output", "b.jsonl"),
            ],
        )
        s.backfill_passages()
    return str(path)


def test_an_inert_store_says_derive_never_ran_and_the_ledger_is_empty(db, capsys):
    assert main(["--db", db, "stats"]) == 0
    out = capsys.readouterr().out
    assert "derive last run : never run" in out
    assert "turns not yet derived : 3" in out  # the tool turn is not evidence
    assert "turns recalled in 7 days : 0" in out
    assert "beliefs recalled in 30 days : 0" in out
    assert "turns ever recalled : 0 of 4 (0.0%)" in out
    assert "last recalled : never" in out
    assert (
        "verdict : ledger empty: derive has never run. `memware derive --plan` previews with "
        "no network call; `memware config derive.auto true` enables it." in out
    )
    assert "verdict : nothing has been recalled in 30 days" in out
    # labeled `field : value`, one per line; no table, no colour
    for line in out.splitlines():
        assert line == "" or " : " in line, line
    assert "\x1b" not in out


def test_json_keeps_the_six_counts_and_adds_derive_and_utilization(db, capsys):
    assert main(["--db", db, "stats", "--json"]) == 0
    captured = capsys.readouterr()
    r = json.loads(captured.out)
    assert {k: r[k] for k in SIX} == {
        "turns": 4,
        "passages": 3 + 1,
        "sessions": 2,
        "beliefs_current": 0,
        "beliefs_total": 0,
        "reviews_open": 0,
    }
    assert r["db"].endswith("m.db")
    assert r["derive"] == {
        "auto": False,
        "state_file": db + ".derive.json",
        "runs": 0,
        "last_run": None,
        "last_run_age_hours": None,
        "watermark": 0,
        "max_turn_id": 3,
        "turns_pending": 3,
    }
    assert r["utilization"] == {
        "beliefs_recalled_7d": 0,
        "beliefs_recalled_30d": 0,
        "turns_recalled_7d": 0,
        "turns_recalled_30d": 0,
        "turns_ever_recalled": 0,
        "turns_ever_recalled_share": 0.0,
        "last_recalled": None,
        "last_recalled_age_hours": None,
    }
    # the fields carry the facts; the verdict is for people only
    assert "verdict" not in captured.out and "ledger empty" not in captured.out
    assert "ledger empty" not in captured.err


def test_utilization_counts_recalls_by_window_from_the_existing_columns(db):
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    with Store(db) as s:
        for turn_id, uses, days in ((1, 3, 2), (2, 1, 20), (3, 1, 90)):
            s.conn.execute(
                "UPDATE turn SET use_count=?, last_used=? WHERE id=?",
                (uses, _iso(now - timedelta(days=days)), turn_id),
            )
        ids = [assert_belief(s, subj, "is", "x").belief_id for subj in ("api", "db", "ci")]
        for belief_id, uses, ago in (
            (ids[0], 2, timedelta(hours=1)),
            (ids[1], 1, timedelta(days=10)),
        ):
            s.conn.execute(
                "UPDATE belief SET use_count=?, last_used=? WHERE id=?",
                (uses, _iso(now - ago), belief_id),
            )
        u = s.utilization(now=now)
    assert u == {
        "beliefs_recalled_7d": 1,
        "beliefs_recalled_30d": 2,
        "turns_recalled_7d": 1,
        "turns_recalled_30d": 2,
        "turns_ever_recalled": 3,
        "turns_ever_recalled_share": 0.75,
        "last_recalled": _iso(now - timedelta(hours=1)),  # the newest across both tables
        "last_recalled_age_hours": 1.0,
    }


def test_stats_reads_the_derive_state_beside_the_store(db, capsys):
    md.save_state(
        md.state_path(db),
        {"watermark": 2, "runs": 5, "last_run": _iso(datetime.now(UTC) - timedelta(days=40))},
    )
    assert main(["--db", db, "stats", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)["derive"]
    assert (d["runs"], d["watermark"], d["max_turn_id"], d["turns_pending"]) == (5, 2, 3, 1)
    assert 40 * 24 - 1 < d["last_run_age_hours"] < 40 * 24 + 1

    assert main(["--db", db, "stats"]) == 0
    out = capsys.readouterr().out
    assert "ledger empty" not in out  # it ran and found nothing: a different state
    assert "derive runs : 5" in out
    assert (
        "verdict : derive last ran 40 days ago and derive.auto is off (1 turn not yet derived). "
        "`memware derive --apply` catches up; `memware config derive.auto true` keeps it current."
        in out
    )

    main(["--db", db, "config", "derive.auto", "true"])
    capsys.readouterr()
    assert main(["--db", db, "stats"]) == 0
    out = capsys.readouterr().out
    assert "derive auto : on" in out
    assert "derive.auto is off" not in out


def test_a_stale_derive_with_nothing_waiting_is_not_flagged(db, capsys):
    md.save_state(
        md.state_path(db),
        {"watermark": 3, "runs": 1, "last_run": _iso(datetime.now(UTC) - timedelta(days=40))},
    )
    assert main(["--db", db, "stats"]) == 0
    assert "derive.auto is off" not in capsys.readouterr().out


def test_a_working_store_prints_no_verdict(db, capsys):
    main(["--db", db, "assert", "api", "listens on", "8443"])
    md.save_state(md.state_path(db), {"watermark": 3, "runs": 1, "last_run": now_iso()})
    main(["--db", db, "recall", "api listens 8443"])
    capsys.readouterr()
    assert main(["--db", db, "stats"]) == 0
    out = capsys.readouterr().out
    assert "verdict" not in out
    assert "turns ever recalled : 2 of 4 (50.0%)" in out


def test_an_empty_store_prints_no_verdict(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "empty.db"), "stats"]) == 0
    out = capsys.readouterr().out
    assert "verdict" not in out
    assert "turns ever recalled : 0 of 0" in out


def test_prompt_hook_injection_is_not_counted_as_a_recall(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "h.db")
    main(["--db", db, "assert", "api", "port", "8443"])
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "which api port"})))
    assert main(["--db", db, "context", "--from-hook"]) == 0
    assert "8443" in capsys.readouterr().out  # the hook did inject the belief
    with Store(db) as s:
        row = s.conn.execute("SELECT use_count, last_used FROM belief").fetchone()
    assert (row["use_count"], row["last_used"]) == (0, None)

    main(["--db", db, "recall", "api port", "--what", "beliefs"])  # an agent or a person asked
    with Store(db) as s:
        assert s.conn.execute("SELECT use_count FROM belief").fetchone()[0] == 1


def test_age_hours_is_utc_arithmetic():
    now = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
    assert age_hours("2026-07-01T12:00:00Z", now=now) == 1.0
    assert age_hours(None) is None
    assert age_hours("not a time") is None
