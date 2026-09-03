from memware.index import (
    ELLIPSIS,
    _join_passages,
    fts_query,
    read_turns,
    search_beliefs,
    search_turns,
)
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


def test_snippet_windows_the_match_not_the_head(store, tmp_path):
    p = tmp_path / "long.jsonl"
    filler = "unrelated preamble sentence. " * 40
    write_claude_jsonl(
        p,
        "long",
        [
            (
                "assistant",
                "2026-08-30T00:00:00Z",
                filler + "the registry token rotates every 90 days",
            )
        ],
    )
    sync_file(store, p, harness="claude-code")
    [h] = search_turns(store, "registry token rotates", k=1)
    assert h.snippet is not None and "90 days" in h.snippet and "preamble" not in h.snippet


def test_recall_ranks_the_passage_not_the_diluted_turn(store, tmp_path):
    """A value buried in a long turn must outrank a short turn that merely shares words."""
    p = tmp_path / "mixed.jsonl"
    buried = (
        "we talked about the deploy script and the cache and the linter. " * 90
        + "the registry token rotates every 90 days. "
        + "then we moved on to unrelated scheduling work. " * 90
    )
    write_claude_jsonl(
        p,
        "mixed",
        [
            ("assistant", "2026-08-30T00:00:00Z", buried),
            ("assistant", "2026-08-30T00:01:00Z", "the registry is a thing we use"),
        ],
    )
    sync_file(store, p, harness="claude-code")
    hits = search_turns(store, "how often does the registry token rotate", k=2)
    assert "90 days" in hits[0].text
    assert len(hits[0].text) < len(buried) / 4  # a passage, not the whole turn
    assert hits[0].offset > 0 and hits[0].passage_id is not None


def test_recall_returns_one_hit_per_turn(store, tmp_path):
    p = tmp_path / "one.jsonl"
    write_claude_jsonl(
        p, "one", [("assistant", "2026-08-30T00:00:00Z", "the build cache lives here. " * 400)]
    )
    sync_file(store, p, harness="claude-code")
    assert store.stats()["passages"] > 1
    hits = search_turns(store, "build cache", k=8)
    assert len(hits) == 1 and len({h.id for h in hits}) == 1


def test_a_hit_id_reads_back_the_whole_turn(store, tmp_path):
    p = tmp_path / "read.jsonl"
    long_turn = "chatter about nothing. " * 200 + "the deploy key expires in March"
    write_claude_jsonl(p, "read", [("assistant", "2026-08-30T00:00:00Z", long_turn)])
    sync_file(store, p, harness="claude-code")
    [hit] = search_turns(store, "deploy key expires", k=1)
    [turn] = read_turns(store, "read", around=hit.id, window=0)
    assert turn["text"] == long_turn
    assert hit.text in long_turn and len(hit.text) < len(long_turn)


def test_recall_quotes_two_passages_when_the_answer_is_not_where_the_words_are(store, tmp_path):
    """The question's vocabulary and the answer often sit in different parts of one turn."""
    p = tmp_path / "split.jsonl"
    turn = (
        "we spent a while on the registry token and how the registry is configured. " * 30
        + "unrelated filler about scheduling and the linter. " * 40
        + "anyway it rotates every 90 days."
    )
    write_claude_jsonl(p, "split", [("assistant", "2026-08-30T00:00:00Z", turn)])
    sync_file(store, p, harness="claude-code")
    [hit] = search_turns(store, "registry token", k=1, passages_per_turn=2)
    assert "registry token" in hit.text
    assert len(hit.text) < len(turn)
    [one] = search_turns(store, "registry token", k=1, passages_per_turn=1, record_use=False)
    assert len(one.text) < len(hit.text)


def test_non_adjacent_passages_are_joined_by_an_ellipsis():
    adjacent = [
        {"ord": 3, "passage_text": "first half "},
        {"ord": 4, "passage_text": "second half"},
    ]
    assert _join_passages(adjacent) == "first half second half"
    gapped = [
        {"ord": 0, "passage_text": "the question words"},
        {"ord": 7, "passage_text": "the answer"},
    ]
    assert _join_passages(gapped) == f"the question words{ELLIPSIS}the answer"
    assert _join_passages([]) == ""


def test_prompt_injection_requires_a_subject_match(store):
    assert_belief(
        store, "agentmemory", "decision", "replace with memware", valid_from="2026-09-02T00:00:00Z"
    )
    # a prompt that merely contains the relation word must not pull the belief in
    assert (
        search_beliefs(
            store, "draft a follow-up decision card for the keyboard", require_subject=True
        )
        == []
    )
    assert [
        h.subject
        for h in search_beliefs(store, "what did we decide about agentmemory", require_subject=True)
    ] == ["agentmemory"]
    # explicit recall stays broad
    assert search_beliefs(store, "decision card")


def test_multi_query_fusion_finds_what_single_phrasings_rank_low(store, tmp_path):
    from memware.index import search_turns_multi

    p = tmp_path / "m.jsonl"
    turns = [
        (
            "assistant",
            "2026-08-20T00:00:00Z",
            "the gateway health probe listens on port 8443 behind the proxy",
        )
    ]
    turns += [
        (
            "assistant",
            "2026-08-21T00:00:00Z",
            f"notes about the proxy configuration round {i} and its health",
        )
        for i in range(12)
    ]
    turns += [
        ("assistant", "2026-08-22T00:00:00Z", f"port allocation table revision {i} for the cluster")
        for i in range(12)
    ]
    write_claude_jsonl(p, "m", turns)
    sync_file(store, p, harness="claude-code")
    fused = search_turns_multi(
        store, ["proxy health", "port 8443", "which port does the gateway listen on"], k=3
    )
    assert "8443" in fused[0].text
    assert search_turns_multi(store, ["", "  "], k=3) == []
