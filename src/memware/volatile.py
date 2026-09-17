"""Beliefs that were true when recorded and need re-checking now, and what injection leaves out.

A belief closes only when a later value supersedes it, which is right for a decision and wrong
for a snapshot. A row count, the version a branch was at, a PR's status: each was true the day
it was written and is wrong a day later, while nothing ever supersedes it. This module names
those classes with no model call, so ``derive`` rejects them before they are written and the
two unsolicited readers (the prompt hook and the session-start digest) leave out the ones an
older store already holds.

Three classes, each decided from the triple alone:

* **measurement**: a bare quantity (a number with an optional unit or magnitude word, or an
  "N of M" figure) under a relation that is a measurement noun ("row count", "test count",
  "null rate"). A relation that names a setting ("retry limit", "batch size") is never a
  measurement: a configured number stays true until someone changes it.
* **moving version**: a version string for something named as current, built, installed,
  deployed or on a branch, and not as pinned or required.
* **status**: anything about a numbered PR, issue, ticket or run ("PR #12", "memware #22"),
  including what it contains, or a status word ("merged", "failing") under a status relation.

Two rules need the project a session runs in, whose manifest (``pyproject.toml``,
``package.json``, ``Cargo.toml``) declares a version, and apply to beliefs whose subject names
that project:

* **contradicted**: a belief about the project's own version whose value is not the manifest's.
* **older version**: a belief that names an older version of the project beside its name
  ("memware 0.4.0 known issue") is history once the manifest is newer.

A person stating a fact is a decision to keep it. A belief with reliability above what derive
writes, or a source that is not a ``memware:session/`` pointer, is exempt from all of it. Nothing
here writes: a left-out belief stays in ``memware beliefs``, recall and the MCP tools, marked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple

from memware.config import get_dotted, load_config
from memware.index import _subject_terms

DERIVED_RELIABILITY = 0.5
"""What ``memware derive`` writes (``memware.derive.RELIABILITY``); anything above is a person's."""
DERIVED_SOURCE = "memware:session/"

MEASUREMENT = "measurement"
MOVING_VERSION = "moving_version"
STATUS = "status"
CONTRADICTED = "contradicted"
OLDER_VERSION = "older_version"
CLASSES = (MEASUREMENT, MOVING_VERSION, STATUS)
REASONS = (CONTRADICTED, OLDER_VERSION, *CLASSES)
"""Every reason injection leaves a belief out, in the order they are checked."""

WINDOW_KEY = "inject.volatile_days"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def wordset(words: str) -> frozenset[str]:
    """A word list written as prose-width lines rather than one quoted word per line."""
    return frozenset(words.split())


# ---------------------------------------------------------------------------
# the three classes
# ---------------------------------------------------------------------------
# A false positive costs more than a false negative. At derive it only means a weaker ledger,
# but at injection it silently hides a durable fact someone relied on. So a relation with a
# setting word is config whatever else it says, and a noun that names a setting as often as a
# measurement ("size", "count", "length", "rate") is a measurement only beside a counted thing.
_QUANTITY = re.compile(
    r"^(?:~|≈|<=?|>=?|about|approx\.?|approximately|around|roughly|nearly|almost|over|under"
    r"|at least|at most|more than|less than|fewer than)?\s*"
    r"[-+]?\d[\d,_]*(?:\.\d+)?"
    r"(?:\s*(?:/|of|out of)\s*\d[\d,_]*(?:\.\d+)?)?"  # "3 of 5", "3/5"
    r"\s*(?:%|[a-z]{1,3}\b)?"  # "83%", "340ms", "1.2gb", "4.2m", "512Mi"
    r"(?:\s+[a-z][a-z-]*){0,2}\s*$",  # "91 tests", "4.2 million rows"
    re.I,
)

SETTING_NOUNS = wordset(
    """
    limit limits max maximum min minimum default defaults initial desired pool page line sample
    batch chunk buffer window request worker workers timeout timeouts retry retries cap ceiling
    floor budget interval ttl retention concurrency parallel parallelism quota setting settings
    configured pinned pin required allowed expiry backoff target keep capacity port ports
    threshold per replicas heap context
    """
)
"""A relation with one of these names a configured value: "page size", "sample rate", "worker
count", "memory request", "context length", "default state"."""
IDENTIFIER_NOUNS = wordset("id ids uid gid pid key index code sha hash name number")
"""A number under one of these identifies something ("user id", "issue number"); it counts nothing."""
MEASURE_NOUNS = wordset(
    """
    percentage percent pct ratio coverage cardinality throughput latency p50 p90 p95 p99 elapsed
    runtime took accuracy average avg mean median removed deleted added processed remaining
    pending passed failed skipped succeeded null usage uptime downtime utilization backlog lag
    """
)
"""Nouns that are a measurement whenever the value is a bare quantity: "null rate", "p95 latency",
"disk usage", "rows removed"."""
AMBIGUOUS_MEASURES = wordset(
    """
    count counts total totals size sizes rate rates length lengths duration durations time times
    speed score scores sum amount volume frequency memory disk bytes
    """
)
"""Nouns that name a setting as often as a measurement ("pool size", "line length"): a measurement
only beside a counted thing, in the relation or the value ("row count", "size: 4.2 million rows")."""
COUNTED_NOUNS = wordset(
    """
    row rows record records test tests duplicate duplicates account accounts file files entry
    entries item items user users session sessions turn turns belief beliefs document documents
    commit commits lines error errors failure failures occurrence occurrences event events message
    messages run runs job jobs task tasks download downloads install installs star stars view
    views visitor visitors hit hits check checks pages samples requests issues tickets customers
    orders transactions
    """
)
"""What a data quantity counts: a relation naming one ("rows backfilled", "test count") with a
bare quantity is a measurement."""

_VERSION = re.compile(r"^v?\d+(?:\.\d+){1,3}(?:[-+.]?[0-9a-z]+(?:\.[0-9a-z]+)*)?$", re.I)
VERSION_NOUNS = frozenset({"version", "versions", "release", "tag"})
MOVING = wordset(
    """
    current currently latest newest built build installed install deployed running main master
    head trunk branch wheel sdist published shipped live now today
    """
)
PINNING = wordset(
    """
    pinned pin pins required requires minimum min maximum max supported compatible locked lock
    constraint floor ceiling target targets declared desired default
    """
)

_TRACKED = re.compile(
    r"(?:^|[\s(])#\d+\b"
    r"|\b(?:pr|prs|pull request|mr|merge request|issue|ticket|run|job|check|workflow)\s*#?\d+\b",
    re.I,
)
STATUS_NOUNS = wordset(
    "status state progress stage phase result results outcome conclusion verdict health"
)
STATUS_FILLER = wordset(
    "current currently overall latest last build ci review merge deploy deployment release pr check checks run the of"
)
"""Words a relation made only of status words may also carry: "build status", "current state"."""
_OPEN_ISSUE = re.compile(
    r"^(?:(?:known|open|outstanding|current|active|remaining)\s+)?(?:issue|issues|bug|bugs|blocker|blockers)$"
)
"""Relations that name what is wrong right now: "known issue", "open issue", "blocker"."""
STATUS_VALUES = wordset(
    """
    open opened closed merged unmerged draft pending passing passed failing failed green red
    running queued blocked approved done complete completed success successful succeeded
    cancelled canceled skipped broken fixed resolved unresolved landed stale started waiting ready
    reverted abandoned flaky errored archived
    """
) | frozenset(
    {
        "in progress",
        "in review",
        "changes requested",
        "not started",
        "on hold",
        "ready for review",
        "timed out",
    }
)


def names_setting(relation: str) -> bool:
    """Whether the relation names a configured value ("retry limit", "batch size")."""
    return bool(_words(relation) & SETTING_NOUNS)


def is_measurement(relation: str, value: str) -> bool:
    """A bare quantity that measures something: under a measurement noun ("null rate"), a counted
    noun ("rows backfilled"), or an ambiguous one beside a counted noun ("row count", "size: 4.2
    million rows"). Never under a setting or identifier noun."""
    if not _QUANTITY.match(value.strip()):
        return False
    words = _words(relation)
    if words & (SETTING_NOUNS | IDENTIFIER_NOUNS):
        return False
    return bool(
        words & (MEASURE_NOUNS | COUNTED_NOUNS)
        or (words & AMBIGUOUS_MEASURES and _words(value) & COUNTED_NOUNS)
    )


def is_version(value: str) -> bool:
    return bool(_VERSION.match(value.strip()))


def is_moving_version(subject: str, relation: str, value: str) -> bool:
    words = _words(f"{subject} {relation}")
    return (
        is_version(value)
        and bool(words & VERSION_NOUNS)
        and bool(words & MOVING)
        and not words & PINNING
    )


def is_status(subject: str, relation: str, value: str) -> bool:
    """About a numbered PR, issue, ticket or run; or a relation made only of status words
    ("status", "build status", "progress", "known issue"), whatever the value; or a status word
    under a status noun. A setting word makes it config: "default state", "initial state"."""
    if _TRACKED.search(f"{subject} {relation}"):
        return True
    words = _words(relation)
    if words & SETTING_NOUNS:
        return False
    if _OPEN_ISSUE.match(" ".join(re.findall(r"[a-z]+", relation.lower()))):
        return True
    if words & STATUS_NOUNS and words <= STATUS_NOUNS | STATUS_FILLER:
        return True
    state = " ".join(re.findall(r"[a-z]+", value.lower()))
    return bool(words & STATUS_NOUNS) and state in STATUS_VALUES


def classify(subject: str, relation: str, value: str) -> str | None:
    """The volatile class of a triple (:data:`CLASSES`), or None for a durable one. Regex and
    word lists only: it runs in ``derive``'s gate and on every prompt."""
    if is_measurement(relation, value):
        return MEASUREMENT
    if is_moving_version(subject, relation, value):
        return MOVING_VERSION
    if is_status(subject, relation, value):
        return STATUS
    return None


def human_stated(reliability: object, source: object, confirmed: object = False) -> bool:
    """A person asserted it: reliability above derive's, a source that is not a session pointer
    (``remember`` and ``memware assert`` take free text, or none), or a person confirmed the
    derived belief since (``confirmed``, from the ``confirmation`` table: the same value
    asserted again, or a review approval)."""
    try:
        above = float(str(reliability)) > DERIVED_RELIABILITY
    except ValueError:
        above = False
    return bool(confirmed) or above or not str(source or "").startswith(DERIVED_SOURCE)


def row_human_stated(row: Any) -> bool:
    """:func:`human_stated` for a belief row; ``confirmed`` counts when the query selected it."""
    columns = row.keys()  # a sqlite3.Row: `in` would search its values, not its column names
    confirmed = row["confirmed"] if "confirmed" in columns else False
    return human_stated(row["reliability"], row["source"], confirmed)


def volatility(row: Any) -> str | None:
    """The mark a belief row carries in ``memware beliefs``, recall and the MCP tools: its class
    when derive wrote it, None when a person stated or confirmed it, or it is durable."""
    if row_human_stated(row):
        return None
    return classify(str(row["subject"]), str(row["relation"]), str(row["value"]))


# ---------------------------------------------------------------------------
# versions and the manifest
# ---------------------------------------------------------------------------
def _release(version: str) -> tuple[int, ...] | None:
    m = re.match(r"v?(\d+(?:\.\d+)*)", version.strip(), re.I)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def older_version(version: str, than: str) -> bool | None:
    """Whether ``version`` is older than ``than``, compared as versions, never as strings (as
    strings, "0.10.0" sorts before "0.4.0"). None when either cannot be read as a version."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # memware has no runtime dependencies; packaging is usually there
        pass
    else:
        try:
            return Version(version) < Version(than)
        except InvalidVersion:
            pass
    a, b = _release(version), _release(than)
    if a is None or b is None:
        return None
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


OWN_VERSION_WORDS = (
    VERSION_NOUNS
    | MOVING
    | wordset(
        """
    released package repo repository project app cli library lib dist distribution pypi npm
    crate local checkout source tree is at on the of
    """
    )
)
"""The words a belief about a project's own version may use besides the project's name. A word
outside it names something else ("memware ruff version", "memware python version")."""


def _norm_version(v: str) -> str:
    return v.strip().lower().removeprefix("v")


def manifest_rule(
    subject: str, relation: str, value: str, names: Iterable[str], version: str
) -> tuple[str, str] | None:
    """(:data:`OLDER_VERSION` or :data:`CONTRADICTED`, the version read from the belief) for a
    belief about the project that its manifest ``version`` overrules, or None."""
    names = [n for n in names if n]
    terms = _subject_terms(" ".join(names))
    if not terms or not _subject_terms(subject) & terms:
        return None
    text = f"{subject} {relation}"
    for term in sorted(terms, key=len, reverse=True):
        pattern = rf"(?<![\w.-]){re.escape(term)}[\s@_-]*v?(\d+(?:\.\d+){{1,3}})\b"
        for m in re.finditer(pattern, text, re.I):
            if older_version(m.group(1), version):
                return OLDER_VERSION, m.group(1)
    rest = _words(text) - _words(" ".join(names))
    if (
        rest & VERSION_NOUNS
        and rest <= OWN_VERSION_WORDS
        and is_version(value)
        and _norm_version(value) != _norm_version(version)
    ):
        return CONTRADICTED, value.strip()
    return None


# ---------------------------------------------------------------------------
# the injection gate
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    reason: str
    """One of :data:`REASONS`."""
    detail: str
    """Why, in words: "pyproject.toml says 0.6.1", "a quantity measured once"."""

    def __str__(self) -> str:
        return f"{label(self.reason)}: {self.detail}"


def parse_days(raw: object) -> float | None:
    """A non-negative number of days, or None for anything else (``7d``, ``-3``, ``true``)."""
    if isinstance(raw, bool):
        return None
    try:
        days = float(str(raw).strip())
    except ValueError:
        return None
    return days if days >= 0 and days == days and days != float("inf") else None


def window_days(cfg: dict[str, Any] | None = None) -> float:
    """``inject.volatile_days``: a volatile derived belief younger than this many days is still
    injected. 0, the default, never injects one. ``memware config`` refuses a value that is not a
    non-negative number; one written by hand reads as 0."""
    return parse_days(get_dotted(cfg if cfg is not None else load_config(), WINDOW_KEY)) or 0.0


def _age_days(ts: object, now: datetime) -> float | None:
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return (now - t).total_seconds() / 86400.0


class Declared(NamedTuple):
    """A version a manifest declares, with the package name beside it."""

    name: str
    version: str
    path: str
    """The file it was read from, relative to the project root."""


def is_placeholder(version: str) -> bool:
    """``0.0.0`` and its tagged forms (``0.0.0-development``): a stand-in some release tooling
    writes, never a version to check a belief against."""
    return bool(re.match(r"^v?0+(?:\.0+){1,3}(?:$|[-+])", version.strip()))


@dataclass(frozen=True)
class Gate:
    """What an unsolicited injection leaves out, for one project. ``names`` and ``declared`` come
    from :func:`memware.digest.resolve_project`; with nothing declared only the classes apply."""

    names: tuple[str, ...] = ()
    declared: tuple[Declared, ...] = ()
    volatile_days: float = 0.0
    now: datetime | None = None

    def declared_for(self, subject: str) -> Declared | None:
        """The version to check a belief about ``subject`` against: the manifest whose package
        name the subject names, else the only one declared. Two that the subject does not tell
        apart check nothing."""
        terms = _subject_terms(subject)
        named = [d for d in self.declared if d.name and _subject_terms(d.name) & terms]
        if named:
            return named[0]
        return self.declared[0] if len(self.declared) == 1 else None

    def verdict(self, row: Any) -> Verdict | None:
        """Why ``row`` (a belief row) is left out, or None to inject it."""
        if row_human_stated(row):
            return None
        subject, relation, value = str(row["subject"]), str(row["relation"]), str(row["value"])
        declared = self.declared_for(subject)
        if declared is not None:
            ruled = manifest_rule(subject, relation, value, self.names, declared.version)
            if ruled is not None:
                reason, named = ruled
                where = f"{declared.path} says {declared.version}"
                if reason == OLDER_VERSION:
                    return Verdict(reason, f"names {named}, {where}")
                return Verdict(reason, where)
        cls = classify(subject, relation, value)
        if cls is None:
            return None
        if self.volatile_days:
            age = _age_days(row["valid_from"], self.now or datetime.now(UTC))
            if age is not None and age < self.volatile_days:
                return None
        return Verdict(cls, DESCRIBE[cls])

    def admits_hit(self, volatile: str | None, valid_from: str | None) -> bool:
        """For a reader that has only a recall hit (the Hermes provider): its ``volatile`` mark,
        honouring the window. No manifest rule: a hit does not say which project it is in."""
        if volatile is None:
            return True
        if not self.volatile_days:
            return False
        age = _age_days(valid_from, self.now or datetime.now(UTC))
        return age is not None and age < self.volatile_days


DESCRIBE = {
    MEASUREMENT: "a quantity measured once",
    MOVING_VERSION: "what something was at when recorded",
    STATUS: "the state of an issue, PR, run or check",
}


def label(reason: str) -> str:
    """``moving_version`` -> ``moving version``: the words a person reads for a reason key."""
    return reason.replace("_", " ")
