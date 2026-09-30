from memware.eval import run
from memware.ingest import sync_file
from memware.ledger import assert_belief
from memware.store import Store
from tests.conftest import write_claude_jsonl


def test_eval_scores_beliefs_and_evidence_separately(tmp_path):
    db = tmp_path / "e.db"
    with Store(db) as s:
        assert_belief(s, "api", "port", "8080", valid_from="2026-01-01T00:00:00Z")
        assert_belief(s, "api", "port", "8443", valid_from="2026-02-01T00:00:00Z")
        p = tmp_path / "old.jsonl"
        write_claude_jsonl(
            p, "old", [("assistant", "2026-01-02T00:00:00Z", "the api port is 8080 for now")]
        )
        sync_file(s, p, harness="claude-code")
    q = tmp_path / "q.jsonl"
    q.write_text(
        '{"id":"1","question":"api port","expect_any":["8443"],"not_expect":["8080"],"type":"stale"}\n'
        '{"id":"2","question":"zebra habitat","expect_any":["savanna"],"type":"negative"}\n'
    )
    rep = run(str(db), q)
    assert rep["beliefs"]["accuracy"] == 1.0 and rep["beliefs"]["stale_rate"] == 0.0
    # transcripts are evidence: the old value may appear there, but the current belief
    # leads the context, so positional scoring does not count it as stale
    assert rep["beliefs+turns"]["stale_rate"] == 0.0
    assert rep["beliefs"]["by_type"] == {"stale": 1.0, "negative": 1.0}
    assert rep["results"][0]["beliefs+turns"]["found"]


def test_stale_only_when_old_value_leads(tmp_path):
    from memware.eval import _score

    q = {"expect_any": ["8443"], "not_expect": ["8080"], "type": "stale"}
    assert _score(q, "The port is 8443 (it was 8080 until March).")["ok"]
    assert not _score(q, "The port is 8080; some notes say 8443.")["ok"]
    assert not _score(q, "The port is 8080.")["ok"]


def test_corpus_flag_builds_a_clean_store(tmp_path):
    import json

    from memware.eval import build_clean_store
    from memware.store import Store
    from tests.conftest import write_claude_jsonl

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_claude_jsonl(
        corpus / "real.jsonl",
        "r",
        [("assistant", "2026-08-10T00:00:00Z", "the api port is 8443 since the migration")],
    )
    write_claude_jsonl(
        corpus / "evalrun.jsonl",
        "e",
        [
            ("user", "2026-08-11T00:00:00Z", "[memware-eval] which api port"),
            ("assistant", "2026-08-11T00:00:01Z", "8443"),
        ],
    )
    live = tmp_path / "live.db"
    with Store(live) as s:
        assert_belief(s, "api", "port", "8443")
    built = build_clean_store(str(tmp_path / "scratch.db"), corpus, beliefs_from=str(live))
    assert built == {"files": 2, "turns": 1, "beliefs": 1}
    q = tmp_path / "q.jsonl"
    q.write_text(
        json.dumps({"id": "1", "question": "api port", "expect_any": ["8443"], "type": "fact"})
        + "\n"
    )
    rep = run(str(tmp_path / "scratch.db"), q)
    assert rep["beliefs"]["accuracy"] == 1.0


def test_also_skip_markers(tmp_path):
    from memware.eval import build_clean_store
    from tests.conftest import write_claude_jsonl

    corpus = tmp_path / "c"
    corpus.mkdir()
    write_claude_jsonl(
        corpus / "old-eval.jsonl",
        "o",
        [
            (
                "user",
                "2026-08-01T00:00:00Z",
                "Answer briefly using only what you know: which api port",
            )
        ],
    )
    write_claude_jsonl(
        corpus / "real.jsonl",
        "r",
        [("assistant", "2026-08-02T00:00:00Z", "the api port is 8443 after the migration")],
    )
    built = build_clean_store(
        str(tmp_path / "s.db"), corpus, also_skip=["Answer briefly using only what you know"]
    )
    assert built["turns"] == 1


def _eval_fixture(tmp_path):
    """A store with a superseded port belief, and a question set covering all three types."""
    import json

    db = tmp_path / "e.db"
    with Store(db) as s:
        assert_belief(s, "api", "port", "8080", valid_from="2026-01-01T00:00:00Z")
        assert_belief(s, "api", "port", "8443", valid_from="2026-02-01T00:00:00Z")
    q = tmp_path / "q.jsonl"
    rows = [
        {"id": "1", "question": "api port", "expect_any": ["8443"], "not_expect": ["8080"]},
        {"id": "2", "question": "zebra habitat", "expect_any": ["savanna"], "type": "negative"},
    ]
    q.write_text("\n\n".join(json.dumps(r) for r in rows) + "\n")  # blank lines are skipped
    return db, q


def test_eval_main_prints_a_summary_per_context(tmp_path, capsys):
    from memware.eval import main

    db, q = _eval_fixture(tmp_path)
    assert main([str(q), "--db", str(db)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("[beliefs] n=2 accuracy=1.000 stale_rate=0.000")
    assert out[1].startswith("[beliefs+turns] n=2 accuracy=1.000")
    assert "beliefs injected on 50% of questions (0% of negatives)" in out[2]
    assert "median_latency=" in out[2]


def test_eval_main_json_carries_every_result(tmp_path, capsys):
    import json

    from memware.eval import main

    db, q = _eval_fixture(tmp_path)
    assert main([str(q), "--db", str(db), "--json", "-k", "3"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["n"] == 2
    assert [r["id"] for r in rep["results"]] == ["1", "2"]
    assert rep["results"][0]["type"] == "fact"  # the default when a row names none
    assert rep["results"][0]["beliefs_injected"] is True
    assert rep["results"][1]["beliefs_injected"] is False
    assert rep["beliefs_injection_rate_negatives"] == 0.0


def test_eval_summary_of_an_empty_question_set_is_zeroes_not_a_crash(tmp_path):
    db = tmp_path / "e.db"
    Store(db).close()
    q = tmp_path / "empty.jsonl"
    q.write_text("\n")
    rep = run(str(db), q)
    assert rep["n"] == 0
    assert rep["beliefs"] == {
        "accuracy": 0.0,
        "stale_rate": 0.0,
        "by_type": {},
        "context_chars_median": 0,
    }
    assert rep["latency_ms_median"] == 0.0


def test_eval_main_corpus_rebuilds_a_scratch_store(tmp_path, capsys):
    """--corpus indexes the transcripts into --db (dropping evaluation runs), copies the
    beliefs from --beliefs-from, and reports what it built on stderr."""
    import json

    from memware.eval import MARKER, main

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_claude_jsonl(
        corpus / "real.jsonl",
        "r",
        [("assistant", "2026-08-10T00:00:00Z", "the api port is 8443 since the migration")],
    )
    write_claude_jsonl(
        corpus / "evalrun.jsonl", "e", [("user", "2026-08-11T00:00:00Z", f"{MARKER} which port")]
    )
    live = tmp_path / "live.db"
    with Store(live) as s:
        assert_belief(s, "api", "port", "8443")
    q = tmp_path / "q.jsonl"
    q.write_text(
        json.dumps({"id": "1", "question": "api port", "expect_any": ["8443"], "type": "fact"})
    )
    scratch = tmp_path / "scratch.db"
    rc = main(
        [
            str(q),
            "--db",
            str(scratch),
            "--corpus",
            str(corpus),
            "--beliefs-from",
            str(live),
            "--json",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "clean store: {'files': 2, 'turns': 1, 'beliefs': 1}" in captured.err
    assert json.loads(captured.out)["beliefs"]["accuracy"] == 1.0


def test_eval_main_refuses_to_rebuild_the_default_store(tmp_path, monkeypatch):
    """--corpus deletes --db first, so pointing it at the live store must be refused."""
    import pytest

    import memware.eval as ev

    default = tmp_path / "live" / "memware.db"
    default.parent.mkdir()
    Store(default).close()
    monkeypatch.setattr(ev, "DEFAULT_DB", default)
    q = tmp_path / "q.jsonl"
    q.write_text("\n")
    with pytest.raises(SystemExit) as exc:
        ev.main([str(q), "--db", str(default), "--corpus", str(tmp_path)])
    assert "scratch --db" in str(exc.value)
    assert default.exists()
