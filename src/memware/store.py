"""SQLite schema and connection handling."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS belief (
  id            INTEGER PRIMARY KEY,
  key           TEXT NOT NULL,
  subject       TEXT NOT NULL,
  relation      TEXT NOT NULL,
  value         TEXT NOT NULL,
  valid_from    TEXT NOT NULL,
  valid_to      TEXT,
  recorded_at   TEXT NOT NULL,
  superseded_by INTEGER,
  status        TEXT NOT NULL DEFAULT 'committed',
  reliability   REAL NOT NULL DEFAULT 0.5,
  source        TEXT,
  use_count     INTEGER NOT NULL DEFAULT 0,
  last_used     TEXT
);
CREATE INDEX IF NOT EXISTS belief_key_idx ON belief(key, valid_from);
CREATE VIRTUAL TABLE IF NOT EXISTS belief_fts USING fts5(
  subject, relation, value, content='belief', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS belief_ai AFTER INSERT ON belief BEGIN
  INSERT INTO belief_fts(rowid, subject, relation, value)
  VALUES (new.id, new.subject, new.relation, new.value);
END;
CREATE TRIGGER IF NOT EXISTS belief_ad AFTER DELETE ON belief BEGIN
  INSERT INTO belief_fts(belief_fts, rowid, subject, relation, value)
  VALUES ('delete', old.id, old.subject, old.relation, old.value);
END;
CREATE TRIGGER IF NOT EXISTS belief_au AFTER UPDATE OF subject, relation, value ON belief BEGIN
  INSERT INTO belief_fts(belief_fts, rowid, subject, relation, value)
  VALUES ('delete', old.id, old.subject, old.relation, old.value);
  INSERT INTO belief_fts(rowid, subject, relation, value)
  VALUES (new.id, new.subject, new.relation, new.value);
END;

CREATE TABLE IF NOT EXISTS turn (
  id        INTEGER PRIMARY KEY,
  session   TEXT NOT NULL,
  seq       INTEGER NOT NULL,
  ts        TEXT,
  role      TEXT NOT NULL,
  text      TEXT NOT NULL,
  source    TEXT NOT NULL,
  harness   TEXT NOT NULL,
  use_count INTEGER NOT NULL DEFAULT 0,
  last_used TEXT,
  UNIQUE(source, seq)
);
CREATE INDEX IF NOT EXISTS turn_session_idx ON turn(session, seq);

-- Turns are ranked through their passages (see memware.passage): a ~400-token
-- span of one turn, anchored by turn_id and start_char. Only passages are
-- indexed; the turn text stays whole for reading a session back.
CREATE TABLE IF NOT EXISTS passage (
  id         INTEGER PRIMARY KEY,
  turn_id    INTEGER NOT NULL,
  ord        INTEGER NOT NULL,
  start_char INTEGER NOT NULL,
  end_char   INTEGER NOT NULL,
  text       TEXT NOT NULL,
  UNIQUE(turn_id, ord)
);
CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(
  text, content='passage', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS passage_ai AFTER INSERT ON passage BEGIN
  INSERT INTO passage_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS passage_ad AFTER DELETE ON passage BEGIN
  INSERT INTO passage_fts(passage_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS turn_ad_passages AFTER DELETE ON turn BEGIN
  DELETE FROM passage WHERE turn_id = old.id;
END;

CREATE TABLE IF NOT EXISTS cursor (
  source     TEXT PRIMARY KEY,
  offset     INTEGER NOT NULL,
  seq        INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review (
  id           INTEGER PRIMARY KEY,
  belief_id    INTEGER NOT NULL,
  incumbent_id INTEGER,
  reason       TEXT NOT NULL,
  opened_at    TEXT NOT NULL,
  decided_at   TEXT,
  decision     TEXT,
  external_ref TEXT
);
"""

DEFAULT_DB = Path(os.environ.get("MEMWARE_DB", "~/.memware/memware.db")).expanduser()

SCHEMA_VERSION = 1
"""Bumped when an existing file needs work beyond ``CREATE ... IF NOT EXISTS``.

1. Passages replace whole-turn FTS: ``turn_fts`` is dropped and every existing
   turn is chunked into the ``passage`` table on first open.
"""


def now_iso() -> str:
    """Current UTC time as ISO-8601 with second precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """One SQLite database holding turns, beliefs, cursors and reviews.

    The connection is opened in WAL mode so readers never block on writers;
    every writer keeps its transaction short.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path else DEFAULT_DB
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        if not self._has_fts5():
            raise RuntimeError("this SQLite build lacks FTS5; memware requires it")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an older file up to :data:`SCHEMA_VERSION`. Runs once, in one transaction.

        A store written before version 1 indexed whole turns in ``turn_fts``. That
        table and its triggers go, and every turn already on disk is chunked — a
        few seconds for a 30-day corpus, and only on the first open after upgrade.
        """
        if int(self.conn.execute("PRAGMA user_version").fetchone()[0]) >= SCHEMA_VERSION:
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DROP TRIGGER IF EXISTS turn_ai")
            self.conn.execute("DROP TRIGGER IF EXISTS turn_ad")
            self.conn.execute("DROP TABLE IF EXISTS turn_fts")
            self.backfill_passages()
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            if self.conn.in_transaction:
                self.conn.execute("COMMIT")

    def backfill_passages(self, *, batch: int = 1000) -> int:
        """Chunk every turn that has no passages yet. Returns the number of turns done."""
        from memware.passage import index_turn

        done, after = 0, -1
        while True:
            rows = self.conn.execute(
                "SELECT t.id, t.text FROM turn t LEFT JOIN passage p ON p.turn_id = t.id "
                "WHERE t.id > ? AND p.id IS NULL ORDER BY t.id LIMIT ?",
                (after, batch),
            ).fetchall()
            if not rows:
                return done
            for r in rows:
                index_turn(self.conn, int(r["id"]), r["text"])
            after = int(rows[-1]["id"])
            done += len(rows)

    def _has_fts5(self) -> bool:
        row = self.conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
        return bool(row and row[0])

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def stats(self) -> dict[str, int]:
        q = self.conn.execute
        return {
            "turns": int(q("SELECT count(*) FROM turn").fetchone()[0]),
            "passages": int(q("SELECT count(*) FROM passage").fetchone()[0]),
            "sessions": int(q("SELECT count(DISTINCT session) FROM turn").fetchone()[0]),
            "beliefs_current": int(
                q(
                    "SELECT count(*) FROM belief WHERE valid_to IS NULL AND status='committed'"
                ).fetchone()[0]
            ),
            "beliefs_total": int(q("SELECT count(*) FROM belief").fetchone()[0]),
            "reviews_open": int(
                q("SELECT count(*) FROM review WHERE decision IS NULL").fetchone()[0]
            ),
        }
