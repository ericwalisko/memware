"""Beliefs from another project's world, which the prompt hook leaves out.

The subject rule (:func:`memware.index.subject_passes`) lets a belief in on one word its subject
shares with the prompt when that word is rare across all of the user's conversations. Rarity is
read as the word is written, and the passage index stores stems, so an ordinary word the stemmer
changes ("decision" is stored as "decis") reads as rare. That is deliberate and measured
(docs/relevance-calibration.md): it is how project names, also stored as stems, keep their facts.
Its cost is that a belief derived in one project can reach a prompt in an unrelated one on a
single ordinary word, whatever the belief is about.

Where the belief came from cannot settle this alone. A belief cites the session it was derived
in, and most sessions run in worktrees that are gone by the time the belief is injected, so
"derived in another directory" is true of most beliefs that belong to the project. What does
settle it is whether the belief belongs to this project's world: whether this project's
conversations ever use the subject's other words.

A belief is :func:`foreign` to the project a session runs in, and left out, when all of these hold:

* it was derived from a Claude Code session whose transcript lies outside the project's
  transcript directories (:func:`memware.digest.transcript_dirs`: the primary checkout and every
  live worktree), and no person stated or confirmed it;
* its subject shares exactly one word with the prompt (two shared words always pass, as before);
* the project has at least :data:`MIN_PASSAGES` indexed passages, so a word's absence means
  something;
* that one word is ordinary here: more than :data:`ORDINARY_SHARE` of the project's passages hold
  it. A word this project seldom uses is a deliberate reference to something outside it, and a
  belief reached through one passes ("the digware app", asked from another repository);
* the subject has another word that is not a number, and none of those words appears in any of
  the project's passages.

Counting here is stem-aware (``MATCH`` over ``passage_fts``): "has this project ever talked about
treatments" must find "treatment". Anything in doubt keeps today's behaviour: a person's belief, a
source that is not a session pointer, a session memware no longer holds, another harness's
session, a small project, a subject with no other words, or a query FTS5 refuses.

The digest needs none of this: it matches only beliefs whose subject names the project itself.
Hermes ``prefetch`` runs outside any project, so it has no project to compare with.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from memware.digest import _source_ranges
from memware.index import _subject_terms
from memware.ledger import pointer_session, pointer_turn
from memware.volatile import row_human_stated

MIN_PASSAGES = 300
"""Below this many indexed passages in the project, nothing is left out. Absence is the evidence
the rule reads, and a few conversations have not used most words yet."""

ORDINARY_SHARE = 0.02
"""A shared word in more than this share of the project's passages (one in fifty) is part of how
the project talks, not a reference to another one."""

_NUMBER = re.compile(r"^[\d._/-]+$")


class ProjectScope:
    """The project a session runs in, as the turn ranges its transcripts occupy, with the passage
    counts :func:`foreign` reads. Counts are read lazily and kept for this object's life."""

    def __init__(self, conn: sqlite3.Connection, dirs: Iterable[Path]) -> None:
        self.conn = conn
        self.ranges = _source_ranges(dirs)
        self._passages: int | None = None
        self._held: dict[tuple[str, int], bool | None] = {}
        self._where = " OR ".join(["(t.source >= ? AND t.source < ?)"] * len(self.ranges))
        self._bounds = [b for pair in self.ranges for b in pair]

    def holds(self, source: str) -> bool:
        """Whether a transcript path lies in one of the project's directories."""
        return any(lo <= source < hi for lo, hi in self.ranges)

    @property
    def passages(self) -> int:
        if self._passages is None:
            self._passages = (
                int(
                    self.conn.execute(
                        "SELECT count(*) FROM passage p JOIN turn t ON t.id = p.turn_id "
                        f"WHERE {self._where}",
                        self._bounds,
                    ).fetchone()[0]
                )
                if self.ranges
                else 0
            )
        return self._passages

    def held(self, term: str, at_least: int) -> bool | None:
        """Whether at least ``at_least`` of the project's passages hold ``term`` or a word with
        its stem; None when FTS5 refuses the term. The count stops at ``at_least``, so a word
        common everywhere costs no more than the first few project passages that hold it."""
        key = (term, at_least)
        if key not in self._held:
            phrase = '"' + term.replace('"', '""') + '"'
            try:
                n = int(
                    self.conn.execute(
                        "SELECT count(*) FROM (SELECT 1 FROM passage_fts f "
                        "JOIN passage p ON p.id = f.rowid JOIN turn t ON t.id = p.turn_id "
                        f"WHERE passage_fts MATCH ? AND ({self._where}) LIMIT ?)",
                        [phrase, *self._bounds, at_least],
                    ).fetchone()[0]
                )
            except sqlite3.OperationalError:
                self._held[key] = None
            else:
                self._held[key] = n >= at_least
        return self._held[key]

    def ordinary(self, term: str) -> bool | None:
        """Whether more than :data:`ORDINARY_SHARE` of the project's passages hold ``term``."""
        return self.held(term, int(self.passages * ORDINARY_SHARE) + 1)

    def absent(self, term: str) -> bool | None:
        """Whether no passage of the project holds ``term`` or a word with its stem."""
        found = self.held(term, 1)
        return None if found is None else not found


def _origin(conn: sqlite3.Connection, source: object) -> tuple[str, str] | None:
    """(transcript path, harness) of the session a derived belief cites, or None."""
    turn = pointer_turn(source)
    if turn is None:
        return None
    row = conn.execute("SELECT source, harness FROM turn WHERE id = ?", (turn,)).fetchone()
    if row is None:  # the turn was re-synced or pruned: any turn of the same session will do
        row = conn.execute(
            "SELECT source, harness FROM turn WHERE session = ? LIMIT 1",
            (pointer_session(source),),
        ).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def foreign(scope: ProjectScope, row: Any, query_terms: set[str]) -> bool:
    """Whether the belief ``row`` (with ``source``, ``reliability`` and, when selected,
    ``confirmed``) is from another project's world for a prompt whose terms are ``query_terms``.
    See the module docstring for the rule."""
    if row_human_stated(row):
        return False
    origin = _origin(scope.conn, row["source"])
    if origin is None or origin[1] != "claude-code" or scope.holds(origin[0]):
        return False
    subject = _subject_terms(row["subject"])
    shared = subject & query_terms
    if len(shared) != 1:
        return False
    others = [t for t in subject - shared if not _NUMBER.match(t)]
    if not others or scope.passages < MIN_PASSAGES:
        return False
    if scope.ordinary(next(iter(shared))) is not True:
        return False
    return all(scope.absent(t) is True for t in others)
