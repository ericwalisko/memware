"""User and agent text reaches FTS5 ``MATCH`` in recall, the prompt hook and the digest. It goes
through :func:`memware.index.fts_query`, which keeps only word-shaped tokens and quotes each one, so
no FTS5 syntax (column filters, NEAR, prefix stars, boolean operators, bare quotes) survives into
the query, and none of it can error, read another column, or run long. See docs/security.md."""

import random
import re

from memware.digest import project_beliefs
from memware.index import (
    MAX_TERM_CHARS,
    fts_query,
    search_beliefs,
    search_turns,
    subject_passes,
)
from memware.ledger import assert_belief
from memware.passage import index_turn

QUERY_SHAPE = re.compile(r'"[^"\s]+"(?: OR "[^"\s]+")*')

HOSTILE = [
    '"',
    '""',
    'a"b" OR "c',
    "subject : secret",
    "value:secret",
    "{subject value}: x",
    "- subject : x",
    "NEAR(port config, 2)",
    "port* ^config",
    "port AND NOT config",
    "(port OR",
    "x OR",
    "__ ___x",
    "a\x00b",
    "\u200bport",
    "ｐｏｒｔ",
    "'; DROP TABLE belief; --",
    "belief_fts MATCH 'x'",
    "*",
    "^",
    "+",
    ":",
    "…",
    "a." * 5000,
]


def _seed(store):
    for i, text in enumerate(["the memware port is 8080", "config lives in ~/.memware/config"]):
        cur = store.conn.execute(
            "INSERT INTO turn(session, seq, ts, role, text, source, harness) "
            "VALUES ('s1', ?, '2026-01-01T00:00:00Z', 'user', ?, 'src', 'test')",
            (i, text),
        )
        index_turn(store.conn, int(cur.lastrowid), text)
    assert_belief(store, "memware port", "is", "8080")
    assert_belief(store, "memware secret", "value", "hunter22")


def _exercise(store, text: str) -> None:
    q = fts_query(text)
    assert q == "" or QUERY_SHAPE.fullmatch(q), (text[:80], q[:200])
    search_turns(store, text, record_use=False)
    search_beliefs(store, text, record_use=False)
    search_beliefs(store, text, record_use=False, require_subject=True)
    project_beliefs(store.conn, [text])
    subject_passes(store, text, "memware port")


def test_hostile_queries_never_error_and_stay_quoted(store):
    _seed(store)
    for text in HOSTILE:
        _exercise(store, text)


def test_fuzzed_queries_never_error_and_stay_quoted(store):
    _seed(store)
    rng = random.Random(20260928)
    atoms = [*"\"'():*^{}+-.,/_\\ \t\n\x00é\u200b\u202eａ…", "NEAR", "OR", "AND", "NOT"]
    atoms += ["subject", "value", "relation", "text", "port", "memware", "8080", "rowid"]
    for _ in range(1500):
        _exercise(store, "".join(rng.choice(atoms) for _ in range(rng.randint(0, 30))))


def test_a_column_filter_in_the_query_is_only_a_word(store):
    """``value : hunter22`` must not become a filter on the value column: the belief is found
    because its words match, the same as for ``value hunter22``."""
    _seed(store)
    assert fts_query("value : hunter22") == '"value" OR "hunter22"'
    assert fts_query("subject:(secret)") == '"subject" OR "secret"'


def test_a_runaway_token_is_bounded():
    """A dotted or slashed run is one keyword, which FTS5 reads as a phrase of its pieces. Terms
    longer than ``MAX_TERM_CHARS`` are left out, so no query is a phrase of thousands of tokens."""
    for text in ("a." * 100_000, "x/" * 100_000, "a" * 100_000, "a-" * 50_000 + " port"):
        q = fts_query(text)
        terms = [t.strip('"') for t in q.split(" OR ")] if q else []
        assert all(len(t) <= MAX_TERM_CHARS for t in terms), [len(t) for t in terms]
    assert fts_query("a-" * 50_000 + " port") == '"port"'
    assert fts_query("see /Users/me/src/memware/ingest/claude_code.py") == (
        '"see" OR "users/me/src/memware/ingest/claude_code.py"'
    )
