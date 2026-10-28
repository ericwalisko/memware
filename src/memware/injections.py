"""What the prompt hook injected, read back from Claude Code's transcripts.

Claude Code writes the context a ``UserPromptSubmit`` hook adds into the session transcript: an
``attachment`` record whose ``attachment.type`` is ``hook_additional_context``, with the hook's
text in ``attachment.content``. Its ``parentUuid`` chain leads back to the prompt it answered,
past other attachments and the meta records Claude Code adds around a turn. So every injection
can be measured after the fact, whatever ``relevance.mode`` says, and memware stores nothing new
to do it: the garden skill labels these prompt-fact pairs the way it labels the relevance
filter's log, and both join on the same ids (:func:`memware.relevance.prompt_id`).

Each injected line is matched back to its belief by rendering the ledger the way the hook did
(:func:`memware.digest.belief_line`). A superseded or retracted belief still matches, because the
pair is about what reached the prompt then. A line nothing renders to (a belief since pruned, or
an older memware that printed a different line) is counted and left out.

Read-only. It reads the sessions a person started: a headless entrypoint is passed over, as
``derive`` passes it over (:data:`memware.derive.HEADLESS_ENTRYPOINTS`), and so is a sidechain.
What memware keeps out of its store stays out here too: the no-capture list, a
``capture.exclude`` glob, and an ignore marker in the transcript's head or in the prompt.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from memware.derive import HEADLESS_ENTRYPOINTS
from memware.digest import CONTEXT_TITLE, belief_line
from memware.ingest import default_skip_markers, kept_out_source
from memware.relevance import fact, prompt_id, sent_prompt

HOOK_RECORD = "hook_additional_context"
HOOK_EVENT = "UserPromptSubmit"
_RECORDED = " (recorded "


@dataclass(frozen=True)
class Injection:
    """One injected line and the prompt it was injected into."""

    ts: str
    session: str | None
    prompt: str
    """The prompt as the relevance filter would send it (:func:`memware.relevance.sent_prompt`)."""
    prompt_id: str
    rank: int
    """Its place in the block, 0 first: the hook prints memware's ranking in order."""
    block: int
    """How many lines the block held."""
    belief_id: int | None
    fact: str | None
    """The matched belief as the relevance filter phrases a candidate; None when unmatched."""

    @property
    def pair_id(self) -> str | None:
        return None if self.belief_id is None else f"{self.prompt_id}:{self.belief_id}"

    def row(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "session": self.session,
            "harness": "claude-code",
            "prompt_id": self.prompt_id,
            "pair_id": self.pair_id,
            "belief_id": self.belief_id,
            "rank": self.rank,
            "block": self.block,
            "prompt": self.prompt,
            "fact": self.fact,
        }


@dataclass
class Counts:
    files: int = 0
    kept_out: int = 0
    headless: int = 0
    blocks: int = 0
    lines: int = 0
    unmatched: int = 0
    no_prompt: int = 0
    by_file: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, int]:
        return {
            "transcripts_read": self.files,
            "transcripts_kept_out": self.kept_out,
            "blocks_headless_skipped": self.headless,
            "blocks": self.blocks,
            "lines": self.lines,
            "lines_unmatched": self.unmatched,
            "blocks_without_prompt": self.no_prompt,
        }


def block_lines(content: object) -> list[str] | None:
    """The injected lines of memware's block in a hook record's content, or None when the record
    holds no memware block. Claude Code stores the content as a list of strings, one per hook
    output; a plain string is read the same way."""
    texts = [content] if isinstance(content, str) else content if isinstance(content, list) else []
    for text in texts:
        if not isinstance(text, str):
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == CONTEXT_TITLE:
                out = []
                for ln in lines[i + 1 :]:
                    if not ln.startswith("- "):
                        break
                    out.append(ln)
                return out
    return None


def _strip_date(line: str) -> str:
    """``- subject relation: value`` without the ``(recorded YYYY-MM-DD)`` the hook appends."""
    cut = line.rfind(_RECORDED)
    return line[:cut] if cut != -1 and line.endswith(")") else line


class BeliefIndex:
    """Every belief in the ledger, superseded and retracted ones included, keyed by the line the
    hook prints for it. Two beliefs can print the same line (a value asserted, retracted and
    asserted again); the one valid at the injection's time wins, else the newest."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._exact: dict[str, list[sqlite3.Row]] = defaultdict(list)
        self._undated: dict[str, list[sqlite3.Row]] = defaultdict(list)
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT id, subject, relation, value, valid_from, valid_to FROM belief "
            "WHERE status != 'rejected'"
        ):
            line = belief_line(r["subject"], r["relation"], r["value"], r["valid_from"])
            self._exact[line].append(r)
            self._undated[_strip_date(line)].append(r)

    @staticmethod
    def _pick(rows: list[sqlite3.Row], ts: str) -> sqlite3.Row:
        valid = [
            r
            for r in rows
            if str(r["valid_from"]) <= ts and (r["valid_to"] is None or ts < str(r["valid_to"]))
        ]
        return max(valid or rows, key=lambda r: int(r["id"]))

    def match(self, line: str, ts: str) -> sqlite3.Row | None:
        rows = self._exact.get(line) or self._undated.get(_strip_date(line))
        return self._pick(rows, ts) if rows else None


def _typed_text(record: dict[str, object]) -> str | None:
    """The text of a user record a person typed; None for a tool result or a meta record."""
    if record.get("type") != "user" or record.get("isMeta"):
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        texts = [
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(texts) if texts else None
    return None


def _records(path: Path) -> list[dict[str, object]]:
    out = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from a killed session
            if isinstance(d, dict):
                out.append(d)
    return out


def _prompt_for(record: dict[str, object], by_uuid: dict[str, dict[str, object]]) -> str | None:
    """The typed prompt a hook record answered: up its parent chain, past attachments and meta
    records, to the first user record a person typed."""
    seen = set()
    cur = record
    while True:
        parent = cur.get("parentUuid")
        if not isinstance(parent, str) or parent in seen or parent not in by_uuid:
            return None
        seen.add(parent)
        cur = by_uuid[parent]
        if cur.get("type") == "user":
            text = _typed_text(cur)
            if text is not None:
                return text
            if not cur.get("isMeta"):
                return None  # a tool result: the hook did not answer a typed prompt


def read_transcript(
    path: Path, index: BeliefIndex, counts: Counts, since: str | None, markers: list[str]
) -> Iterator[Injection]:
    records = _records(path)
    by_uuid = {str(r["uuid"]): r for r in records if isinstance(r.get("uuid"), str)}
    for r in records:
        att = r.get("attachment")
        if r.get("type") != "attachment" or not isinstance(att, dict):
            continue
        if att.get("type") != HOOK_RECORD or att.get("hookEvent") != HOOK_EVENT:
            continue
        lines = block_lines(att.get("content"))
        if lines is None:
            continue  # another hook's context
        ts = str(r.get("timestamp") or "")
        if since is not None and ts < since:
            continue
        if r.get("entrypoint") in HEADLESS_ENTRYPOINTS or r.get("isSidechain"):
            counts.headless += 1
            continue
        prompt = _prompt_for(r, by_uuid)
        if prompt is None:
            counts.no_prompt += 1
            continue
        if any(m in prompt for m in markers):
            continue
        counts.blocks += 1
        sent = sent_prompt(prompt)
        pid = prompt_id(sent)
        session = r.get("sessionId")
        for rank, line in enumerate(lines):
            counts.lines += 1
            b = index.match(line, ts)
            if b is None:
                counts.unmatched += 1
            yield Injection(
                ts=ts,
                session=str(session) if session else None,
                prompt=sent,
                prompt_id=pid,
                rank=rank,
                block=len(lines),
                belief_id=None if b is None else int(b["id"]),
                fact=None if b is None else fact(b["subject"], b["relation"], b["value"]),
            )


def scan(
    conn: sqlite3.Connection,
    src: str | os.PathLike[str],
    *,
    since: datetime | None = None,
) -> tuple[list[Injection], Counts]:
    """Every injection under the transcript tree ``src`` since ``since``, oldest first."""
    counts = Counts()
    index = BeliefIndex(conn)
    markers = default_skip_markers()
    cut = None if since is None else since.isoformat().replace("+00:00", "Z")
    out: list[Injection] = []
    root = Path(src).expanduser()
    paths: Iterable[Path] = [root] if root.is_file() else sorted(root.glob("**/*.jsonl"))
    for p in paths:
        if not p.is_file():
            continue
        if since is not None and datetime.fromtimestamp(p.stat().st_mtime, since.tzinfo) < since:
            continue  # nothing written since the window opened
        if kept_out_source(p, markers):
            counts.kept_out += 1
            continue
        counts.files += 1
        found = list(read_transcript(p, index, counts, cut, markers))
        if found:
            counts.by_file[str(p)] = len(found)
        out += found
    out.sort(key=lambda i: (i.ts, i.rank))
    return out, counts
