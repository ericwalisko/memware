from memware.index import fts_query, search_beliefs, search_turns
from memware.ingest import sync_file
from memware.ledger import assert_belief
from tests.conftest import write_claude_jsonl


def test_fts_query_strips_stopwords_and_quotes_terms():
    assert fts_query("What is the port for the api?") == '"port" OR "api"'
    assert fts_query("the the a") == ""


def test_turn_recall_ranks_recent_higher_and_records_use(store, tmp_path):
    old, new = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    write_claude_jsonl(
        old,
        "old",
        [("assistant", "2024-01-01T00:00:00Z", "deploy uses the blue-green rollout script")],
    )
    write_claude_jsonl(
        new,
        "new",
        [("assistant", "2026-08-30T00:00:00Z", "deploy uses the blue-green rollout script")],
    )
    sync_file(store, old, harness="claude-code")
    sync_file(store, new, harness="claude-code")
    hits = search_turns(store, "blue-green rollout deploy", k=2)
    assert [h.session for h in hits] == ["new", "old"]
    for _ in range(5):
        search_turns(store, "rollout", k=1, record_use=True)
    assert store.conn.execute("SELECT max(use_count) FROM turn").fetchone()[0] >= 5


def test_belief_recall_never_returns_superseded_values(store):
    assert_belief(store, "api", "port", "8080", valid_from="2026-01-01T00:00:00Z")
    assert_belief(store, "api", "port", "8443", valid_from="2026-02-01T00:00:00Z")
    assert [h.text for h in search_beliefs(store, "api port")] == ["api port 8443"]


def test_empty_query_returns_nothing(store):
    assert search_turns(store, "") == [] and search_beliefs(store, "of the") == []
