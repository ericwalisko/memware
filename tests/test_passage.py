import sqlite3

import pytest

from memware.passage import MAX_TOKENS, MIN_TOKENS, chunk, est_tokens, index_turn
from memware.store import SCHEMA_VERSION, Store


def test_short_text_is_one_passage():
    assert chunk("the api listens on 8443") == [(0, 23)]
    assert chunk("") == []
    assert est_tokens("four chars a token--") == 5


def test_passages_tile_the_text_exactly():
    text = ("a sentence about the deploy script. " * 60) + "\n\n" + ("more prose. " * 200)
    spans = chunk(text)
    assert len(spans) > 1
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(b[0] == a[1] for a, b in zip(spans, spans[1:]))  # no gaps, no overlap
    assert "".join(text[s:e] for s, e in spans) == text


def test_passage_sizes_stay_in_the_target_band():
    text = "token " * 4000
    spans = chunk(text)
    assert len(spans) > 4
    for s, e in spans[:-1]:
        assert MIN_TOKENS <= est_tokens(text[s:e]) <= MAX_TOKENS
    assert est_tokens(text[spans[-1][0] : spans[-1][1]]) <= MAX_TOKENS


@pytest.mark.parametrize("reps", [1, 40, 200, 500])
def test_every_passage_stands_alone_as_one_passage(reps):
    text = "the registry token rotates every 90 days. " * reps
    for s, e in chunk(text):
        assert chunk(text[s:e]) == [(0, e - s)]


def test_boundaries_prefer_the_strongest_natural_break():
    head = "x " * 700  # ~350 tokens: inside the window, short of the target
    text = head + "\n\n" + "y " * 2000
    s, e = chunk(text)[0]
    assert text[s:e].endswith("\n\n")
    # with no paragraph break available, a sentence end is taken instead
    sentences = "one two three four five. " * 200
    s, e = chunk(sentences)[0]
    assert sentences[s:e].endswith(". ")


def test_a_boundary_never_stalls_on_unbreakable_text():
    text = "x" * 9000  # no separator anywhere
    spans = chunk(text)
    assert len(spans) > 1 and all(e > s for s, e in spans)
    assert spans[-1][1] == len(text)


def _add_turn(store, turn_id: int, text: str) -> None:
    store.conn.execute(
        "INSERT INTO turn(id,session,seq,ts,role,text,source,harness) "
        "VALUES (?,'s',?,'2026-08-30T00:00:00Z','assistant',?,'f.jsonl','generic')",
        (turn_id, turn_id, text),
    )


def test_index_turn_rewrites_rather_than_duplicates(store):
    _add_turn(store, 1, "the token rotates")
    assert index_turn(store.conn, 1, "the token rotates") == 1
    assert index_turn(store.conn, 1, "the token rotates") == 1
    assert store.conn.execute("SELECT count(*) FROM passage").fetchone()[0] == 1
    matched = store.conn.execute(
        "SELECT count(*) FROM passage_fts WHERE passage_fts MATCH 'rotates'"
    ).fetchone()[0]
    assert matched == 1


def test_deleting_a_turn_drops_its_passages_and_their_index(store):
    _add_turn(store, 1, "deploy notes. " * 500)
    index_turn(store.conn, 1, "deploy notes. " * 500)
    assert store.conn.execute("SELECT count(*) FROM passage").fetchone()[0] > 1
    store.conn.execute("DELETE FROM turn WHERE id=1")
    assert store.conn.execute("SELECT count(*) FROM passage").fetchone()[0] == 0
    orphans = store.conn.execute(
        "SELECT count(*) FROM passage_fts WHERE passage_fts MATCH 'deploy'"
    ).fetchone()[0]
    assert orphans == 0


def test_backfill_is_bounded_by_the_turns_that_need_it(store):
    for i in range(1, 6):
        _add_turn(store, i, f"turn number {i} about the build cache")
    assert store.backfill_passages(batch=2) == 5
    assert store.backfill_passages(batch=2) == 0


LEGACY_SCHEMA = """
CREATE TABLE turn (
  id INTEGER PRIMARY KEY, session TEXT NOT NULL, seq INTEGER NOT NULL, ts TEXT,
  role TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL, harness TEXT NOT NULL,
  use_count INTEGER NOT NULL DEFAULT 0, last_used TEXT, UNIQUE(source, seq)
);
CREATE VIRTUAL TABLE turn_fts USING fts5(
  text, content='turn', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER turn_ai AFTER INSERT ON turn BEGIN
  INSERT INTO turn_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER turn_ad AFTER DELETE ON turn BEGIN
  INSERT INTO turn_fts(turn_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


def test_opening_a_pre_passage_store_backfills_and_drops_turn_fts(tmp_path):
    path = tmp_path / "legacy.db"
    long_turn = "preamble. " * 400 + "the registry token rotates every 90 days"
    old = sqlite3.connect(path)
    old.executescript(LEGACY_SCHEMA)
    old.execute(
        "INSERT INTO turn(session,seq,ts,role,text,source,harness) "
        "VALUES ('legacy',1,'2026-08-30T00:00:00Z','assistant',?,'legacy.jsonl','generic')",
        (long_turn,),
    )
    old.commit()
    old.close()

    with Store(path) as s:
        assert s.stats()["turns"] == 1
        assert s.stats()["passages"] == len(chunk(long_turn)) > 1
        assert int(s.conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        names = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master").fetchall()}
        assert "turn_fts" not in names and "turn_ai" not in names and "turn_ad" not in names

    with Store(path) as s:  # a second open must not re-chunk or double up
        assert s.stats()["passages"] == len(chunk(long_turn))
        assert s.backfill_passages() == 0
