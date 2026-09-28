"""The MCP tools take their arguments from a model, which may be steered by whatever it last read.
No tool touches a path the caller names (session ids are only SQL parameters), and each argument is
bounded before it reaches SQLite: a window or id past SQLite's integer range, ten thousand
phrasings, or a ``valid_from`` that is not a real time. See docs/security.md."""

import pytest

from memware import mcp_server as m
from memware.ledger import current
from memware.store import Store


@pytest.fixture()
def db(tmp_path) -> str:
    path = str(tmp_path / "t.db")
    with Store(path) as s:
        for i in range(3):
            s.conn.execute(
                "INSERT INTO turn(session, seq, ts, role, text, source, harness) "
                "VALUES ('s1', ?, '2026-01-01T00:00:00Z', 'user', ?, 'src', 'test')",
                (i, f"turn {i} about the memware port"),
            )
    return path


def _first_turn(db: str) -> int:
    with Store(db) as s:
        return int(s.conn.execute("SELECT min(id) FROM turn").fetchone()[0])


def test_read_session_bounds_its_window_and_anchor(db):
    first = _first_turn(db)
    assert m.session_turns(db, "s1", around=10**30) == []
    assert len(m.session_turns(db, "s1", around=first, window=10**30)) == 3
    assert [t["seq"] for t in m.session_turns(db, "s1", around=first, window=-5)] == [0]
    assert m.session_turns(db, "../../etc/passwd") == []


def test_recall_bounds_phrasings_and_k(db, monkeypatch):
    seen: dict[str, object] = {}

    def fake(name):
        def search(store, queries, *, k):
            seen[name] = (list(queries), k)
            return []

        return search

    monkeypatch.setattr(m, "search_turns_multi", fake("turns"))
    monkeypatch.setattr(m, "search_beliefs_multi", fake("beliefs"))
    assert m.recall_hits(db, [f"q{i}" for i in range(10_000)], k=10**9) == []
    for name in ("turns", "beliefs"):
        queries, k = seen[name]
        assert len(queries) == m.MAX_QUERIES
        assert k == m.MAX_K
    m.recall_hits(db, ["port"], k=-3)
    assert seen["turns"][1] == 1


@pytest.mark.parametrize(
    "valid_from", ["9999-12-31T00:00:00Z", "zzz", "2999-01-01", "not a date", "2026-13-45"]
)
def test_remember_refuses_a_valid_from_that_is_not_a_real_time(db, valid_from):
    """The ledger orders a key's values by ``valid_from``: it must be a time, and not ahead."""
    with pytest.raises(ValueError):
        m.remember_belief(db, "memware port", "is", "9999", valid_from=valid_from)
    with Store(db) as s:
        assert current(s, "memware port") == []


def test_remember_takes_a_real_valid_from(db):
    out = m.remember_belief(
        db, "memware port", "is", "8080", valid_from="2026-01-01T09:30:00+02:00"
    )
    assert out["outcome"] == "created"
    with Store(db) as s:
        (b,) = current(s, "memware port")
    assert b["valid_from"] == "2026-01-01T07:30:00Z"
