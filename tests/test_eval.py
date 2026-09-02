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
    # transcripts are evidence: the old value is allowed to appear there
    assert rep["beliefs+turns"]["stale_rate"] == 0.5
    assert rep["beliefs"]["by_type"] == {"stale": 1.0, "negative": 1.0}
