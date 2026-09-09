"""``memware derive`` — fill the belief ledger from the transcripts, on a schedule you don't need.

The ledger has two writers. An agent asserts a belief when it notices one (the ``remember``
tool, ``memware assert``). This is the other: a pass over every turn indexed since the last
pass that finds durable facts and files them as ``(subject, relation, value)`` triples, each
with a source pointer into the transcript and the turn's own event time as ``valid_from``.

Two halves, and the deterministic one decides:

* **regex for recall, model for precision.** A trigger regex finds sentences that look like
  a fact ("is now", "switched to", "pinned", "defaults to" …) and hands the model each one
  with its neighbours. The model turns a region into a triple or rejects it.
* **a deterministic backstop after the model.** ``validate()`` admits a triple only if every
  content word of its value occurs in the excerpt (GROUNDEDNESS), the model echoed the right
  excerpt back (``anchor``), and the shape is a fact rather than an order or a pronoun. A
  model cannot introduce a fact the evidence does not contain; a bad model writes less, not
  wrong. Writes carry reliability 0.5 under ``gate_conflicts``, below any human-stated belief,
  so a challenge to one lands in ``memware review``, never in the ledger.

Providers: ``claude-code`` (default) runs the extraction through the Claude Code CLI on your
own subscription — no key, no endpoint, nothing to configure if ``claude`` is on your PATH.
``openai`` is any OpenAI-compatible chat endpoint via ``OPENAI_BASE_URL`` / ``OPENAI_MODEL`` /
``OPENAI_API_KEY`` (environment, or ``<memware home>/.env``). There is no fallback chain: a
model that was not chosen does not get to write into a ledger a human is expected to trust.

The watermark (the highest turn id considered) lives beside the database in
``<db>.derive.json``, so a scratch database keeps its own state and can never advance the
live one. Runs are incremental and idempotent, which is what makes ``--if-stale`` safe to call
from a session hook: run it whenever the machine is on, it does the right amount of work.

Exit codes: 0 done (or nothing to do); 1 unexpected failure; 2 not configured (no provider
credential, no ``claude`` on PATH); 4 provider unavailable right now (rejected credential,
usage limit) — nothing written, watermark untouched, try again later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from memware import __version__
from memware.config import get_dotted, load_config, memware_home
from memware.ledger import Policy, assert_belief
from memware.store import Store

RELIABILITY = 0.5  # a machine read a transcript: below every human-stated belief
POLICY = Policy.GATE_CONFLICTS

MAX_SESSIONS = 60  # per run; a backlog drains over several runs
MAX_REGIONS_PER_SESSION = 24
MAX_CANDIDATES_PER_SESSION = 8
MAX_REGION_CHARS = 1200  # a runaway "sentence" is a code block, not prose
MAX_TURNS_PER_SESSION = 120

MAX_SUBJECT_CHARS = 80
MAX_RELATION_WORDS = 4
MAX_VALUE_CHARS = 200

EXIT_OK, EXIT_FAIL, EXIT_CONFIG, EXIT_UNAVAILABLE = 0, 1, 2, 4


class ProviderConfigError(RuntimeError):
    """Nothing to call: no credential, no endpoint, or no ``claude`` on PATH (exit 2)."""


class ProviderUnavailable(RuntimeError):
    """The provider refused (rejected credential, usage limit). Fatal for this run: every
    later chunk would fail the same way, and each failed chunk is a paid call. Exit 4, nothing
    written, watermark untouched."""


# ---------------------------------------------------------------------------
# sentence splitting
# ---------------------------------------------------------------------------
TRIGGER = re.compile(
    r"\b(is now|are now|now (?:uses|lives|runs|points)|switched to|moved to|renamed"
    r"|decided|decision|chose|pinned|set to|defaults? to|default is|lives in|lives at"
    r"|always|never|must|instead of|replaced by|replaces|version)\b",
    re.I,
)
# Splitting naively on (?<=[.!?])\s+ breaks inside abbreviations, truncating a claim at
# exactly the point that changes its meaning: "Never ship to the U.S. without a compliance
# review." -> "Never ship to the U.S."
_ABBREV = re.compile(
    r"""(?:^|[\s(\["'])"""
    r"(?:e\.g|i\.e|etc|vs|cf|al|approx|incl|excl|resp|no|fig|ca|mr|mrs|ms|dr|prof|sr|jr|st)\.$",
    re.I,
)
_INITIALISM = re.compile(r"""(?:^|[\s(\["'])(?:[A-Za-z]\.)+$""")
_BOUNDARY = re.compile(r"[.!?]\s+")


def split_sentences(text: str) -> list[str]:
    """Split into sentences without breaking inside an abbreviation."""
    parts: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        head = text[start : m.start() + 1]
        rest = text[m.end() :]
        if _ABBREV.search(head) or _INITIALISM.search(head):
            continue
        if not re.match(r"[A-Z`\"'(\[0-9]", rest):
            continue
        parts.append(head.strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def regions_from_texts(texts: list[str], cap: int = MAX_REGIONS_PER_SESSION) -> list[str]:
    """The regex finds the sentence; the model gets its neighbours too."""
    seen: set[str] = set()
    regions: list[str] = []
    for t in texts:
        sents = split_sentences(t)
        for i, s in enumerate(sents):
            if not TRIGGER.search(s):
                continue
            region = " ".join(sents[max(0, i - 1) : i + 2]).strip()
            region = re.sub(r"\s+", " ", region)[:MAX_REGION_CHARS]
            k = region.lower()
            if len(region) >= 30 and k not in seen:
                seen.add(k)
                regions.append(region)
            if len(regions) >= cap:
                return regions
    return regions


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
ENV_KEYS = (
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPENAI_API_KEY",
    "MEMWARE_DERIVE_MODEL",
    "MEMWARE_DERIVE_PROVIDER",
)


def env_file() -> Path:
    return memware_home() / ".env"


def read_env(paths: list[Path] | None = None) -> dict[str, str]:
    """Merge ``.env`` files (first file wins for a key), then the process environment wins."""
    env: dict[str, str] = {}
    for p in paths if paths is not None else [env_file()]:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip("'\""))
    for k in ENV_KEYS:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


SYSTEM = """\
You extract DURABLE FACTS from excerpts of AI-agent work transcripts, as triples.

A triple is (subject, relation, value):
  subject  the named thing the fact is about  ("the ingest job")
  relation a short attribute, 1-3 words, lower case  ("pinned version")
  value    what that attribute currently is  ("0.22.1")

Keep a triple ONLY if all of these hold:
  - it would still be true and worth knowing WEEKS later, in a different session
  - it is a state of the world, a configuration, a decision, or a location — not
    an event that happened once
  - every word of the value appears in the excerpt. Copy the identifiers,
    numbers, paths, flags and version strings EXACTLY. Never normalise them,
    never expand an abbreviation, never add a fact of your own.
  - the subject names the thing. "the watchdog" is too vague if the excerpt says
    "the ingest watchdog" — use what the excerpt says.

REJECT (keep: false) — expect to reject MOST excerpts:
  - an instruction for the task at hand ("implement X", "run the tests then
    report", "do not switch branches") — orders are not knowledge
  - anything restating a prompt, a plan, a TODO, or what someone was asked to do
  - a description of what happened once ("the run failed", "I fixed the typo")
  - a prediction, an intention, or an option under discussion — only what IS
  - anything whose subject or value would be a pronoun ("it", "this", "that")

Return ONLY a JSON array, one object per excerpt, in the SAME ORDER, with one
object for EVERY excerpt. "anchor" must be the excerpt's first six words copied
EXACTLY — it is how the caller checks alignment.

  [{"n": 1, "anchor": "first six words of excerpt one", "keep": true,
    "subject": "...", "relation": "...", "value": "..."},
   {"n": 2, "anchor": "first six words of excerpt two", "keep": false,
    "subject": "", "relation": "", "value": ""}]

No prose, no markdown fences."""


class Usage:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0


class OpenAIProvider:
    """Any OpenAI-compatible chat endpoint. Configure with OPENAI_BASE_URL, OPENAI_MODEL and
    OPENAI_API_KEY (environment or ``<memware home>/.env``). Eight excerpts per call — long
    arrays came back malformed from small models."""

    name = "openai"
    chunk = 8

    def __init__(self, env: dict[str, str]):
        self.base = (env.get("OPENAI_BASE_URL") or "").rstrip("/")
        self.model = env.get("MEMWARE_DERIVE_MODEL") or env.get("OPENAI_MODEL") or ""
        self.key = env.get("OPENAI_API_KEY", "")
        self.usage = Usage()
        missing = [
            k
            for k, v in (
                ("OPENAI_BASE_URL", self.base),
                ("OPENAI_MODEL", self.model),
                ("OPENAI_API_KEY", self.key),
            )
            if not v
        ]
        if missing:
            raise ProviderConfigError(
                f"provider openai needs {', '.join(missing)} — set them in the environment or "
                f"in {env_file()} (or use --provider claude-code, which needs neither)"
            )

    def describe(self) -> str:
        return f"{self.model} @ {self.base}"

    def _post(self, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
                "User-Agent": f"memware-derive/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)  # type: ignore[no-any-return]
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ProviderUnavailable(
                    f"{self.base} rejected the credential (HTTP {e.code}); "
                    f"check OPENAI_API_KEY in {env_file()}"
                ) from e
            if e.code == 429:
                raise ProviderUnavailable(f"{self.base} is rate-limiting (HTTP 429)") from e
            raise

    def complete(self, system: str, user: str, timeout: int = 120) -> str:
        # Reasoning off, and a budget with headroom: this is mechanical extraction. Not every
        # model accepts the flag (some 400 on it), so a 400 is retried once without it.
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": 3000,
            "temperature": 0,
            "reasoning": {"enabled": False},
        }
        try:
            d = self._post(body, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            body.pop("reasoning")
            d = self._post(body, timeout)
        self.usage.calls += 1
        u = d.get("usage") or {}
        self.usage.prompt_tokens += int(u.get("prompt_tokens") or 0)
        self.usage.completion_tokens += int(u.get("completion_tokens") or 0)
        choice = (d.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        if not content:
            raise RuntimeError(
                f"empty content from {self.model} (finish_reason={choice.get('finish_reason')!r})"
            )
        return str(content)


_LIMIT = re.compile(
    r"hit your (session|weekly|.*) limit|usage limit|rate.?limit|too many requests", re.I
)


class ClaudeCodeProvider:
    """The Claude Code CLI on your own subscription: ``claude -p`` with the API key unset.

    Nothing to configure. Each spawn carries a fixed startup cost, so excerpts go up in
    batches of 24 — a typical pass is a handful of spawns, not one per excerpt. Haiku by
    default: triple extraction is mechanical, and the groundedness gate makes a weaker model
    write less rather than wrong.
    """

    name = "claude-code"
    chunk = 24

    def __init__(self, env: dict[str, str], model: str | None = None, binary: str = "claude"):
        self.model = model or env.get("MEMWARE_DERIVE_MODEL") or "haiku"
        self.binary = binary
        self.usage = Usage()
        if shutil.which(binary) is None:
            raise ProviderConfigError(
                f"provider claude-code needs the `{binary}` CLI on PATH "
                "(https://claude.com/claude-code), or use --provider openai"
            )

    def describe(self) -> str:
        return f"{self.model} via {self.binary} -p (subscription)"

    def complete(self, system: str, user: str, timeout: int = 300) -> str:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # subscription
        argv = [
            self.binary,
            "-p",
            user,
            "--model",
            self.model,
            "--output-format",
            "json",
            "--system-prompt",
            system,
            "--max-turns",
            "1",
        ]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        raw = (p.stdout or "").strip()
        try:
            d = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            d = {}
        text = str(d.get("result") or "") if isinstance(d, dict) else ""
        blob = f"{text}\n{p.stderr or ''}\n{raw if not d else ''}"
        if _LIMIT.search(blob):
            raise ProviderUnavailable(f"claude reported a usage limit: {blob.strip()[:160]!r}")
        if p.returncode != 0 or (isinstance(d, dict) and d.get("is_error")):
            raise RuntimeError(f"claude -p failed (rc {p.returncode}): {(p.stderr or raw)[:200]!r}")
        self.usage.calls += 1
        u = d.get("usage") or {} if isinstance(d, dict) else {}
        self.usage.prompt_tokens += int(u.get("input_tokens") or 0) + int(
            u.get("cache_read_input_tokens") or 0
        )
        self.usage.completion_tokens += int(u.get("output_tokens") or 0)
        if not text:
            raise RuntimeError("claude -p returned no result text")
        return text


def make_provider(name: str, env: dict[str, str], model: str | None = None) -> Any:
    if name == "claude-code":
        return ClaudeCodeProvider(env, model=model)
    if name == "openai":
        if model:
            env = {**env, "MEMWARE_DERIVE_MODEL": model}
        return OpenAIProvider(env)
    raise ProviderConfigError(f"unknown provider {name!r} (claude-code | openai)")


# ---------------------------------------------------------------------------
# the deterministic backstop
# ---------------------------------------------------------------------------
TASK_SHAPE = re.compile(
    r"^(the (task|agent|work|change|implementation)\b.*\bmust be\b"
    r"|implement\b|do not (switch|touch|push|force)\b|run the\b|report back\b)",
    re.I,
)
DEICTIC = {
    "it",
    "this",
    "that",
    "they",
    "he",
    "she",
    "we",
    "you",
    "them",
    "the same",
    "the above",
    "the following",
    "there",
    "here",
    "n/a",
    "none",
}
_STOP = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "to",
    "of",
    "in",
    "on",
    "at",
    "for",
    "and",
    "or",
    "as",
    "by",
    "it",
    "its",
    "this",
    "that",
    "with",
    "from",
    "into",
    "now",
    "not",
    "no",
    "has",
    "have",
    "had",
    "will",
    "s",
    "t",
}


def _words(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()


def anchor_ok(region: str, anchor: str) -> bool:
    """Did the model answer about the excerpt we asked about? A batched call once drifted its
    own numbering; the echoed anchor is how that is caught."""
    if not anchor:
        return False
    want, got = _words(region)[:6], _words(anchor)[:6]
    if not want or not got:
        return False
    overlap = len(set(want) & set(got))
    return overlap >= max(2, min(len(want), len(got)) - 2)


def grounded(value: str, region: str) -> bool:
    """Every content word of the value must occur in the evidence. THIS IS THE BACKSTOP."""
    have = set(_words(region))
    want = [w for w in _words(value) if w not in _STOP]
    if not want:
        return False
    return all(w in have for w in want)


def validate(item: dict[str, Any], region: str) -> tuple[dict[str, str] | None, str | None]:
    """Deterministic admission. Returns (candidate, None) or (None, reason)."""
    if not item.get("keep"):
        return None, "model rejected"
    if not anchor_ok(region, str(item.get("anchor") or "")):
        return None, "MISALIGNED: anchor does not match this excerpt"
    subject = re.sub(r"\s+", " ", str(item.get("subject") or "").strip())
    relation = re.sub(r"\s+", " ", str(item.get("relation") or "").strip()).lower()
    value = re.sub(r"\s+", " ", str(item.get("value") or "").strip())
    if not subject or not relation or not value:
        return None, "incomplete triple"
    if subject.lower() in DEICTIC or value.lower() in DEICTIC:
        return None, "deictic subject or value"
    if len(subject) > MAX_SUBJECT_CHARS:
        return None, "subject too long — a sentence, not a thing"
    if len(relation.split()) > MAX_RELATION_WORDS:
        return None, "relation too long — a clause, not an attribute"
    if len(value) > MAX_VALUE_CHARS:
        return None, "value too long — a summary, not a fact"
    if len(value) < 2:
        return None, "value too short"
    if TASK_SHAPE.match(value):
        return None, "task instruction, not a fact"
    if not grounded(value, region):
        return None, "NOT GROUNDED: value contains words the excerpt does not"
    return {"subject": subject, "relation": relation, "value": value}, None


def parse_model_json(raw: str) -> list[Any]:
    """Parse the array; if that fails, salvage the objects that are intact."""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("["), s.rfind("]")
    if i != -1 and j != -1:
        try:
            got = json.loads(s[i : j + 1])
            return got if isinstance(got, list) else [got]
        except json.JSONDecodeError:
            pass
    salvaged = []
    for m in re.finditer(r"\{[^{}]*\}", s):
        try:
            o = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict) and "n" in o:
            salvaged.append(o)
    if salvaged:
        return salvaged
    raise ValueError(f"no usable JSON in model output: {raw[:200]!r}")


def _call_chunk(provider: Any, chunk: list[str], offset: int, verbose: bool = False) -> list[Any]:
    numbered = "\n\n".join(f"[{offset + i + 1}] {r}" for i, r in enumerate(chunk))
    for attempt in (1, 2):
        try:
            return parse_model_json(provider.complete(SYSTEM, numbered))
        except (ProviderUnavailable, ProviderConfigError):
            raise
        except Exception as e:
            if attempt == 2:
                if verbose:
                    print(
                        f"    ! chunk failed after retry ({type(e).__name__}: {e}) — "
                        f"skipping {len(chunk)} excerpts",
                        file=sys.stderr,
                    )
                return []
            time.sleep(1)
    return []


def derive_triples(
    provider: Any, regions: list[str], verbose: bool = False
) -> list[tuple[int, dict[str, str] | None, str | None]]:
    """[(region_index, candidate_or_None, reject_reason_or_None)] — gate-checked."""
    if not regions:
        return []
    by_n: dict[int, dict[str, Any]] = {}
    size = int(getattr(provider, "chunk", 8))
    for off in range(0, len(regions), size):
        for it in _call_chunk(provider, regions[off : off + size], off, verbose):
            if isinstance(it, dict) and isinstance(it.get("n"), int):
                by_n[it["n"]] = it
    out: list[tuple[int, dict[str, str] | None, str | None]] = []
    for i, region in enumerate(regions, 1):
        item = by_n.get(i)
        if not item:
            out.append((i - 1, None, "no answer for this excerpt"))
            continue
        cand, why = validate(item, region)
        out.append((i - 1, cand, why))
    return out


# ---------------------------------------------------------------------------
# evidence (read only) and the watermark
# ---------------------------------------------------------------------------
def open_readonly(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    """A handle SQLite itself refuses to write through (``PRAGMA query_only``).

    Not a ``file:...?mode=ro`` URI: the store is a WAL database, and a read-only URI open
    fails whenever the ``-wal``/``-shm`` side files are absent, which is the normal state
    between writers on some SQLite builds. ``query_only`` gives the same guarantee — every
    write through this handle raises — without that failure.
    """
    p = Path(db_path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no memware database at {p}")
    conn = sqlite3.connect(str(p), timeout=5)
    conn.execute("PRAGMA query_only = 1")
    conn.row_factory = sqlite3.Row
    return conn


def state_path(db_path: str | os.PathLike[str]) -> Path:
    """Beside the database, so a scratch db can never advance the live one."""
    p = Path(db_path).expanduser()
    return p.with_name(p.name + ".derive.json")


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {"watermark": 0, "runs": 0, "last_run": None}
    if not isinstance(data, dict):
        return {"watermark": 0, "runs": 0, "last_run": None}
    data.setdefault("watermark", 0)
    data.setdefault("runs", 0)
    data.setdefault("last_run", None)
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def last_run_age_hours(state: dict[str, Any]) -> float | None:
    last = state.get("last_run")
    if not last:
        return None
    try:
        t = time.mktime(time.strptime(str(last), "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError:
        return None
    return (time.time() - t) / 3600.0


ROLES = ("user", "assistant")


def sessions_with_new_turns(
    conn: sqlite3.Connection,
    watermark: int,
    roles: tuple[str, ...] = ROLES,
    max_sessions: int = MAX_SESSIONS,
) -> list[str]:
    """Sessions that gained a turn since the last run, oldest new evidence first — so a
    backlog drains in the order the work happened (``valid_from`` is event time)."""
    marks = ",".join("?" * len(roles))
    rows = conn.execute(
        f"SELECT session, MIN(id) AS first_new FROM turn WHERE id > ? AND role IN ({marks}) "
        f"GROUP BY session ORDER BY first_new LIMIT ?",
        (watermark, *roles, max_sessions),
    ).fetchall()
    return [str(r["session"]) for r in rows]


def new_turns(
    conn: sqlite3.Connection,
    session: str,
    watermark: int,
    roles: tuple[str, ...] = ROLES,
    limit: int = MAX_TURNS_PER_SESSION,
) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(roles))
    return list(
        conn.execute(
            f"SELECT id, session, seq, ts, role, text FROM turn "
            f"WHERE session=? AND id > ? AND role IN ({marks}) ORDER BY seq LIMIT ?",
            (session, watermark, *roles, limit),
        ).fetchall()
    )


def max_turn_id(conn: sqlite3.Connection, roles: tuple[str, ...] = ROLES) -> int:
    marks = ",".join("?" * len(roles))
    row = conn.execute(
        f"SELECT MAX(id) AS m FROM turn WHERE role IN ({marks})", tuple(roles)
    ).fetchone()
    return int(row["m"] or 0)


def source_pointer(session: str, turn_id: int) -> str:
    """Where the claim was read; resolves with ``memware read <session> --around <turn_id>``."""
    return f"memware:session/{session}/turn/{turn_id}"


def valid_from_of(turn: Any) -> str | None:
    """The turn's event time at second precision, or None to let the ledger stamp now."""
    ts = str(turn["ts"] or "").strip()
    if not ts:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", ts)
    return (m.group(1) + "Z") if m else None


def gather_regions(
    conn: sqlite3.Connection, sessions: list[str], watermark: int
) -> list[tuple[str, sqlite3.Row, str]]:
    """(session, owning turn, region) for every trigger region in the new turns of these
    sessions. Regions are built per turn so each keeps the identity of the turn it came
    from — that turn id is the source pointer. Gathered across sessions before any model
    call so the provider can batch as it likes."""
    out: list[tuple[str, sqlite3.Row, str]] = []
    for session in sessions:
        seen: set[str] = set()
        n = 0
        for t in new_turns(conn, session, watermark):
            for region in regions_from_texts(
                [str(t["text"] or "")], cap=MAX_REGIONS_PER_SESSION - n
            ):
                k = region.lower()
                if k in seen:
                    continue
                seen.add(k)
                out.append((session, t, region))
                n += 1
            if n >= MAX_REGIONS_PER_SESSION:
                break
    return out


def candidates_from(
    provider: Any,
    gathered: list[tuple[str, sqlite3.Row, str]],
    verbose: bool = False,
    rejects: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Every admissible triple with its evidence. ``rejects`` tallies reason -> count: a jump in
    "no answer" means the model started truncating; a jump in "NOT GROUNDED" means it started
    inventing — both look like a quiet night from the outside."""
    if not gathered:
        return []
    regions = [g[2] for g in gathered]
    per_session: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for idx, cand, why in derive_triples(provider, regions, verbose=verbose):
        session, turn, region = gathered[idx]
        if cand is None:
            if rejects is not None and why:
                key = why.split(":")[0]
                rejects[key] = rejects.get(key, 0) + 1
            if verbose and why:
                print(f"    reject [{why}] {region[:100]}", file=sys.stderr)
            continue
        if per_session.get(session, 0) >= MAX_CANDIDATES_PER_SESSION:
            continue
        per_session[session] = per_session.get(session, 0) + 1
        out.append(
            {
                **cand,
                "source": source_pointer(session, int(turn["id"])),
                "valid_from": valid_from_of(turn),
                "region": region,
                "session": session,
                "turn_id": int(turn["id"]),
            }
        )
    return out


def dedupe_run(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One assert per (subject, relation, value) per run — repeating a triple would inflate
    the incumbent's use_count, a retrieval signal, from the write path."""
    out, seen = [], set()
    for c in candidates:
        k = (c["subject"].lower(), c["relation"].lower(), c["value"].lower())
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# writing — through the ledger, never through the read handle
# ---------------------------------------------------------------------------
def write_candidate(cand: dict[str, Any], store: Store) -> dict[str, Any]:
    r = assert_belief(
        store,
        cand["subject"],
        cand["relation"],
        cand["value"],
        valid_from=cand.get("valid_from"),
        source=cand["source"],
        reliability=RELIABILITY,
        policy=POLICY,
    )
    return {"outcome": r.outcome.value, "belief_id": r.belief_id, "review_id": r.review_id}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def run(a: argparse.Namespace) -> int:
    db = str(Path(a.db).expanduser())
    sp = Path(a.state).expanduser() if a.state else state_path(db)
    state = load_state(sp)
    say = (lambda *x, **k: None) if a.quiet else print

    if a.if_stale is not None:
        if a.auto and not get_dotted(load_config(), "derive.auto"):
            return EXIT_OK  # hook-driven and not switched on: stay silent
        age = last_run_age_hours(state)
        if age is not None and age < a.if_stale:
            say(f"skipped: last run {age:.1f}h ago (< {a.if_stale}h)")
            return EXIT_OK

    env = read_env()
    cfg = load_config()
    provider_name = (
        a.provider
        or env.get("MEMWARE_DERIVE_PROVIDER")
        or str(get_dotted(cfg, "derive.provider") or "claude-code")
    )
    model = a.model or get_dotted(cfg, "derive.model")
    provider = make_provider(provider_name, env, model=str(model) if model else None)
    watermark = a.since if a.since is not None else int(state["watermark"])

    conn = open_readonly(db)
    try:
        head = max_turn_id(conn)
        sessions = sessions_with_new_turns(conn, watermark, max_sessions=a.max_sessions)
        say(f"db          : {db}")
        say(f"state       : {sp}")
        say(f"watermark   : {watermark} -> {head} ({head - watermark} new turns)")
        say(
            f"sessions    : {len(sessions)} with new turns"
            f"{' (capped)' if len(sessions) >= a.max_sessions else ''}"
        )
        if not sessions:
            if a.apply:
                state.update(
                    watermark=head,
                    runs=state["runs"] + 1,
                    last_run=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                save_state(sp, state)
            return EXIT_OK
        say(f"provider    : {provider.describe()}")
        say(f"mode        : {'APPLY (writes)' if a.apply else 'dry run (no writes)'}\n")
        gathered = gather_regions(conn, sessions, watermark)
    finally:
        conn.close()

    rejects: dict[str, int] = {}
    candidates = dedupe_run(candidates_from(provider, gathered, verbose=a.verbose, rejects=rejects))

    counts: dict[str, int] = {}
    if a.apply and candidates:
        with Store(db) as store:
            for c in candidates:
                r = write_candidate(c, store)
                outcome = str(r.get("outcome", "error"))
                counts[outcome] = counts.get(outcome, 0) + 1
                note = f"  -> {outcome}" + (
                    f" (review {r.get('review_id')})" if outcome == "pending_review" else ""
                )
                say(f"  {c['subject']} | {c['relation']} = {c['value']}")
                say(f"      valid_from {c['valid_from'] or '(now)'}  source {c['source']}{note}")
    else:
        for c in candidates:
            counts["would_write"] = counts.get("would_write", 0) + 1
            say(f"  {c['subject']} | {c['relation']} = {c['value']}")
            say(f"      valid_from {c['valid_from'] or '(now)'}  source {c['source']}")

    if a.apply:
        state.update(
            watermark=head,
            runs=state["runs"] + 1,
            last_run=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        save_state(sp, state)

    u = provider.usage
    say(
        f"\ncandidates  : {len(candidates)}  "
        + "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    say(
        "rejected    : "
        + (
            "  ".join(f"{k}={v}" for k, v in sorted(rejects.items(), key=lambda kv: -kv[1]))
            or "none"
        )
    )
    say(
        f"excerpts    : {len(gathered)}  llm calls: {u.calls}  {u.prompt_tokens:,} in / {u.completion_tokens:,} out"
    )
    if a.apply:
        say(f"watermark   : advanced to {head}")
        say("review      : memware review list   (conflicts wait there, nothing was overwritten)")
    else:
        say("watermark   : NOT advanced (dry run)")
    return EXIT_OK


def cmd_derive(a: argparse.Namespace) -> int:
    """CLI entry (``memware derive``). Exit codes are the contract, see the module docstring."""
    try:
        return run(a)
    except ProviderConfigError as e:
        print(f"memware derive: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except ProviderUnavailable as e:
        print(
            f"memware derive: {e} — nothing written, watermark not advanced; try again later",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    except FileNotFoundError as e:
        print(f"memware derive: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as e:
        print(f"memware derive failed: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAIL


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument(
        "--apply",
        action="store_true",
        help="write candidates and advance the watermark (default: dry run, prints them)",
    )
    sp.add_argument(
        "--provider",
        choices=["claude-code", "openai"],
        help="claude-code (default; the Claude Code CLI on your subscription) or openai "
        "(any OpenAI-compatible endpoint via OPENAI_* env)",
    )
    sp.add_argument("--model", help="model for the provider (claude-code default: haiku)")
    sp.add_argument(
        "--since",
        type=int,
        metavar="TURN_ID",
        help="override the watermark; --since 0 re-derives from the beginning",
    )
    sp.add_argument("--state", metavar="FILE", help="watermark file (default: <db>.derive.json)")
    sp.add_argument("--max-sessions", type=int, default=MAX_SESSIONS, metavar="N")
    sp.add_argument(
        "--if-stale",
        type=float,
        metavar="HOURS",
        help="skip when the last run is younger than HOURS (safe from a session hook)",
    )
    sp.add_argument(
        "--auto",
        action="store_true",
        help="with --if-stale: also skip unless `memware config derive.auto true`",
    )
    sp.add_argument("--quiet", action="store_true", help="print nothing on success or skip")
    sp.add_argument("--verbose", action="store_true", help="print every rejected excerpt")
