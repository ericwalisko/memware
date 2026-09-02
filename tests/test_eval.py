from memware.eval import run
from memware.ledger import assert_belief
from memware.store import Store


def test_eval_scores_fact_stale_and_negative(tmp_path):
    db = tmp_path / "e.db"
    with Store(db) as s:
        assert_belief(s, "api", "port", "8080", valid_from="2026-01-01T00:00:00Z")
        assert_belief(s, "api", "port", "8443", valid_from="2026-02-01T00:00:00Z")
    q = tmp_path / "q.jsonl"
    q.write_text(
        '{"id":"1","question":"api port","expect_any":["8443"],"not_expect":["8080"],"type":"stale"}\n'
        '{"id":"2","question":"zebra habitat","expect_any":["savanna"],"type":"negative"}\n'
    )
    rep = run(str(db), q)
    assert rep["accuracy"] == 1.0 and rep["stale_rate"] == 0.0
    assert rep["by_type"] == {"stale": 1.0, "negative": 1.0}
