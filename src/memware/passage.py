"""Split a turn into passages, so ranking happens on the paragraph, not the transcript.

BM25 scores a whole document. The answer to a question is usually two sentences
inside a turn of several thousand characters, where its term frequency is
diluted by everything else that turn talks about — so a short, vaguely related
turn outranks the one that actually holds the value. Splitting each turn into
~300-500-token passages at ingest makes the unit of ranking the same size as the
unit a prompt quotes, while ``turn_id`` and ``start_char`` keep every passage
anchored to the turn it came from: recall returns passages, reading back a
session still returns whole turns.

Passages tile their turn exactly — no overlap, no gaps — so the turn text is
recoverable from them and a re-chunk is idempotent. Boundaries are chosen at the
strongest natural break available in the window (paragraph, then line, then
sentence, then word), which is what keeps a passage readable on its own.

Sizes are counted in tokens estimated at four characters per token: the usual
English approximation, and deliberately model-free — memware depends on nothing
outside the standard library.
"""

from __future__ import annotations

import sqlite3

CHARS_PER_TOKEN = 4
TARGET_TOKENS = 400
MAX_TOKENS = 500
MIN_TOKENS = 300

# Strongest break first. Each is searched backwards from the target, so a passage
# ends after the last paragraph break in its window if there is one at all.
_SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")


def est_tokens(text: str) -> int:
    """Token count estimated at four characters per token."""
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _break_before(text: str, floor: int, limit: int) -> int:
    """End offset of the strongest natural break in ``[floor, limit)``, else ``limit``."""
    for sep in _SEPARATORS:
        p = text.rfind(sep, floor, limit)
        if p != -1:
            return p + len(sep)
    return limit


def chunk(
    text: str,
    *,
    target_tokens: int = TARGET_TOKENS,
    max_tokens: int = MAX_TOKENS,
    min_tokens: int = MIN_TOKENS,
) -> list[tuple[int, int]]:
    """``(start, end)`` character spans that tile ``text`` exactly, in order.

    Every span but the last ends at a natural break between ``min_tokens`` and
    ``target_tokens``; the last takes whatever remains, up to ``max_tokens``.
    Text shorter than ``max_tokens`` is one passage.
    """
    n = len(text)
    if n == 0:
        return []
    target = max(1, target_tokens) * CHARS_PER_TOKEN
    hard = max(target, max_tokens * CHARS_PER_TOKEN)
    floor = min(max(0, min_tokens) * CHARS_PER_TOKEN, target - 1)
    spans: list[tuple[int, int]] = []
    start = 0
    while n - start > hard:
        end = _break_before(text, start + floor, start + target)
        spans.append((start, end))
        start = end
    spans.append((start, n))
    return spans


def index_turn(conn: sqlite3.Connection, turn_id: int, text: str) -> int:
    """(Re)write the passages of one turn. Returns how many were written."""
    conn.execute("DELETE FROM passage WHERE turn_id=?", (turn_id,))
    spans = chunk(text)
    conn.executemany(
        "INSERT INTO passage(turn_id, ord, start_char, end_char, text) VALUES (?,?,?,?,?)",
        [(turn_id, i, s, e, text[s:e]) for i, (s, e) in enumerate(spans)],
    )
    return len(spans)
