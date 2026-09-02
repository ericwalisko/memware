import json

from memware.ledger import assert_belief, current
from memware.review import JsonlReviewBackend, open_reviews, sync_reviews


def test_jsonl_backend_round_trip(store, tmp_path):
    assert_belief(store, "svc", "owner", "team-a", reliability=0.9)
    r = assert_belief(store, "svc", "owner", "team-b", reliability=0.2)
    assert len(open_reviews(store)) == 1
    outbox, inbox = tmp_path / "out.jsonl", tmp_path / "in.jsonl"
    be = JsonlReviewBackend(outbox, inbox)
    assert sync_reviews(store, be) == {"published": 1, "applied": 0}
    item = json.loads(outbox.read_text().splitlines()[0])
    assert item["candidate_value"] == "team-b" and item["incumbent_value"] == "team-a"
    inbox.write_text(json.dumps({"review_id": r.review_id, "decision": "approve"}) + "\n")
    assert sync_reviews(store, be) == {"published": 0, "applied": 1}
    assert [c["value"] for c in current(store, "svc")] == ["team-b"]
    assert inbox.read_text() == ""
