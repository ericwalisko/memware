import pytest

from memware.ledger import Outcome, Policy, approve, assert_belief, current, history, reject
from memware.store import Store


def vals(store, subject, relation):
    return [(b["value"], b["valid_from"], b["valid_to"]) for b in history(store, subject, relation)]


def test_create_then_supersede_keeps_history_but_hides_it(store):
    r1 = assert_belief(store, "api", "listens on port", "8080", valid_from="2026-01-01T00:00:00Z")
    r2 = assert_belief(store, "api", "listens on port", "8443", valid_from="2026-02-01T00:00:00Z")
    assert (r1.outcome, r2.outcome) == (Outcome.CREATED, Outcome.SUPERSEDED)
    assert [c["value"] for c in current(store, "api")] == ["8443"]
    assert vals(store, "api", "listens on port") == [
        ("8080", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
        ("8443", "2026-02-01T00:00:00Z", None),
    ]


def test_same_value_reinforces_instead_of_duplicating(store):
    assert_belief(store, "Repo", "default branch", "main", reliability=0.4)
    r = assert_belief(store, "repo ", "Default Branch", " MAIN.", reliability=0.9)
    assert r.outcome is Outcome.REINFORCED
    rows = history(store, "repo", "default branch")
    assert len(rows) == 1 and rows[0]["reliability"] == 0.9 and rows[0]["use_count"] == 1


def _build(tmp_path, name, order):
    s = Store(tmp_path / f"{name}.db")
    for value, ts in order:
        assert_belief(s, "svc", "version", value, valid_from=ts, policy=Policy.AUTO)
    out = (vals(s, "svc", "version"), [c["value"] for c in current(s, "svc")])
    s.close()
    return out


def test_order_independence_of_backfill(tmp_path):
    """Evidence arriving out of order yields the same timeline as chronological order."""
    chrono = [
        ("1", "2026-01-01T00:00:00Z"),
        ("2", "2026-02-01T00:00:00Z"),
        ("3", "2026-03-01T00:00:00Z"),
    ]
    shuffled = [chrono[0], chrono[2], chrono[1]]
    reverse = list(reversed(chrono))
    a, b, c = (
        _build(tmp_path, "a", chrono),
        _build(tmp_path, "b", shuffled),
        _build(tmp_path, "c", reverse),
    )
    assert a == b == c
    timeline, cur = b
    assert cur == ["3"]
    assert timeline == [
        ("1", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
        ("2", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        ("3", "2026-03-01T00:00:00Z", None),
    ]


def test_rerunning_a_backfill_is_idempotent(tmp_path):
    order = [("1", "2026-01-01T00:00:00Z"), ("2", "2026-02-01T00:00:00Z")]
    once = _build(tmp_path, "once", order)
    twice = _build(tmp_path, "twice", order + order)
    assert once == twice


def test_weaker_challenger_goes_to_review_and_incumbent_stays_current(store):
    assert_belief(
        store, "db", "engine", "postgres", reliability=0.9, valid_from="2026-01-01T00:00:00Z"
    )
    r = assert_belief(
        store, "db", "engine", "sqlite", reliability=0.3, valid_from="2026-02-01T00:00:00Z"
    )
    assert r.outcome is Outcome.PENDING_REVIEW and r.review_id is not None
    assert [c["value"] for c in current(store, "db")] == ["postgres"]
    approve(store, r.review_id)
    assert [c["value"] for c in current(store, "db")] == ["sqlite"]
    assert vals(store, "db", "engine")[0][2] == "2026-02-01T00:00:00Z"


def test_rejected_candidate_never_surfaces(store):
    assert_belief(store, "db", "engine", "postgres", reliability=0.9)
    r = assert_belief(store, "db", "engine", "mysql", reliability=0.1)
    reject(store, r.review_id)
    assert [c["value"] for c in current(store, "db")] == ["postgres"]
    assert all(b["value"] != "mysql" for b in history(store, "db", "engine"))
    with pytest.raises(LookupError):
        reject(store, r.review_id)


def test_await_confirmation_policy_gates_even_stronger_challengers(store):
    assert_belief(store, "x", "y", "1", reliability=0.2)
    r = assert_belief(store, "x", "y", "2", reliability=0.9, policy=Policy.AWAIT_CONFIRMATION)
    assert r.outcome is Outcome.PENDING_REVIEW
    assert [c["value"] for c in current(store, "x")] == ["1"]


def test_stronger_challenger_supersedes_under_default_policy(store):
    assert_belief(store, "x", "y", "1", reliability=0.5, valid_from="2026-01-01T00:00:00Z")
    r = assert_belief(store, "x", "y", "2", reliability=0.5, valid_from="2026-01-02T00:00:00Z")
    assert r.outcome is Outcome.SUPERSEDED
    assert [c["value"] for c in current(store, "x")] == ["2"]


def test_reliability_bounds(store):
    with pytest.raises(ValueError):
        assert_belief(store, "a", "b", "c", reliability=1.5)
