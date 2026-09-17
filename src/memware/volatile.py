"""Beliefs that were true when recorded and need re-checking now, and what injection leaves out.

A belief closes only when a later value supersedes it, which is right for a decision and wrong
for a snapshot. A row count, the version a branch was at, a PR's status: each was true the day
it was written and is wrong a day later, while nothing ever supersedes it. This module names
those classes with no model call, so ``derive``'s gate rejects them and the unsolicited readers
(the prompt hook, the session-start digest, the Hermes provider) leave out the ones a store
already holds.

**Precision over recall.** Hiding a durable fact is a new harm: it silently removes something
someone relied on, and nothing tells them. A stale belief that slips through is the old
behaviour, and ``memware beliefs retract ID`` removes it. So only unambiguous cases are volatile,
and anything in doubt is durable. The fuzzy judgment belongs to derive's prompt, where the model
sees the excerpt; this sees only a triple. ``tests/data/volatility_cases.jsonl`` is the labeled
corpus: no durable case may classify volatile, and the volatile cases these rules miss are kept
there, marked, so the tradeoff stays visible.

A qualifier anywhere in the subject or relation always means durable: slo, sla, target,
threshold, budget, commitment, fail under, min, max, limit, default, initial, final, required,
desired, every, schedule, check, and words like them (:data:`QUALIFIERS`). Past that:

* **measurement**: a bare quantity that is one of: a count, total or "number of" over an
  accumulating noun (rows, records, tests, files, lines, commits, duplicates, accounts, users,
  downloads); a magnitude word or a comma-grouped number of 1,000 or more beside such a noun;
  an "N of M" figure over one, or over a completion word ("backfilled", "passed"); or a relation
  that is exactly progress, coverage or null rate.
* **moving version**: a version string under a version noun that the subject or relation calls
  current, latest, built, installed, deployed, released, or on main.
* **status**: a relation that is exactly status, state or progress, whose value is a status word
  (open, merged, review, blocked, failing, archived, …) or whose subject names an instance (an
  id such as ``#12`` or ``t_cd03d14d``, or ending in run, scan, build, job, PR, issue or card).

Two rules need the project a session runs in, and apply to a belief whose subject names the
package that declares the version (``pyproject.toml``, ``package.json``, ``Cargo.toml``):

* **contradicted**: a belief about that package's own version whose value is not the declared one.
* **older version**: a belief that names an older version of the package beside its name
  ("memware 0.4.0 known issue") is history once the declared version is newer.

A person stating a fact is a decision to keep it. A belief with reliability above what derive
writes, a source that is not a ``memware:session/`` pointer, or a confirmation by a person since,
is exempt from all of it. Nothing here writes: a left-out belief stays in ``memware beliefs``,
recall and the MCP tools, marked.
"""

from __future__ import annotations

import re
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
def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


QUALIFIERS = wordset(
    """
    slo sla target targets threshold thresholds budget commitment committed goal expected fail
    under min minimum max maximum limit limits cap ceiling floor quota default defaults initial
    final first original required require requires desired every schedule scheduled cron check
    checks policy rule allowed pinned pin configured setting settings config per page shown
    displayed listed visible keep kept retained retention ttl timeout timeouts interval window
    pool batch chunk buffer sample request worker workers context capacity plan included
    """
)
"""A word that makes a belief a rule, a target or a setting, whatever else it says: "p99 latency
slo", "uptime commitment", "fail under coverage", "final state", "runs every", "status check"."""

_QUANTITY = re.compile(
    r"^(?:~|≈|<=?|>=?|about|approx\.?|approximately|around|roughly|nearly|almost|over|under"
    r"|at least|at most|more than|less than|fewer than)?\s*"
    r"[-+]?\d[\d,_]*(?:\.\d+)?"
    r"(?:\s*(?:/|of|out of)\s*\d[\d,_]*(?:\.\d+)?)?"  # "3 of 5", "3/5"
    r"\s*(?:%|[a-z]{1,3}\b)?"  # "83%", "340ms", "1.2gb", "4.2m"
    r"(?:\s+[a-z][a-z-]*){0,2}\s*$",  # "91 tests", "4.2 million rows"
    re.I,
)
_MAGNITUDE = re.compile(r"\b(?:thousand|million|billion|trillion)\b|\b\d{1,3}(?:,\d{3})+\b", re.I)
"""A magnitude word, or a comma-grouped number (so 1,000 or more)."""
_N_OF_M = re.compile(r"^\s*\d[\d,]*\s*(?:of|/|out of)\s*\d[\d,]*\b", re.I)

ACCUMULATING_PLURAL = wordset(
    "rows records tests files lines commits duplicates accounts users downloads"
)
ACCUMULATING = ACCUMULATING_PLURAL | wordset(
    "row record test file line commit duplicate account user download"
)
"""Nouns a store, a suite or a repository accumulates: a count of them is a snapshot."""
COUNT_WORDS = wordset("count counts total totals tally")
COMPLETION_WORDS = wordset(
    "passed failed done completed complete processed migrated backfilled removed deleted imported indexed remaining succeeded synced"
)
EXACT_MEASURES = frozenset({"progress", "coverage", "null rate", "test coverage", "code coverage"})

_VERSION = re.compile(r"^v?\d+(?:\.\d+){1,3}(?:[-+.]?[0-9a-z]+(?:\.[0-9a-z]+)*)?$", re.I)
VERSION_NOUNS = frozenset({"version", "versions", "release", "tag"})
MOVING = wordset("current currently latest newest built installed deployed released")
_ON_MAIN = re.compile(r"\bon (?:main|master)\b", re.I)

STATUS_RELATIONS = frozenset({"status", "state", "progress"})
_STATUS_LEAD = wordset("current overall latest")
STATUS_VALUE_WORDS = wordset(
    """
    open opened closed merged unmerged review reviewing blocked failing failed passing passed
    archived done pending green red draft queued running cancelled canceled approved rejected
    stale stuck waiting started complete completed progress
    """
)
_STATUS_FILLER = wordset("and but still now in not yet")
INSTANCE_NOUNS = wordset(
    "run runs scan scans build builds job jobs pr prs issue issues card cards ticket tickets mr"
)
_INSTANCE_ID = re.compile(r"#\d+\b|\bt_[0-9a-f]{6,}\b", re.I)


def names_setting(relation: str) -> bool:
    """Whether the relation names a configured value ("retry limit", "worker count")."""
    return bool(set(_tokens(relation)) & QUALIFIERS)


def _qualified(subject: str, relation: str) -> bool:
    return bool(set(_tokens(f"{subject} {relation}")) & QUALIFIERS)


def is_measurement(subject: str, relation: str, value: str) -> bool:
    """An unambiguous measurement; see the module docstring. A qualifier makes it durable."""
    if not _QUANTITY.match(value.strip()) or _qualified(subject, relation):
        return False
    rel = _tokens(relation)
    words = set(rel) | set(_tokens(value))
    if " ".join(rel) in EXACT_MEASURES:
        return True
    counted = (set(rel) & COUNT_WORDS or "number of" in " ".join(rel)) and words & ACCUMULATING
    grouped = _MAGNITUDE.search(value) and words & ACCUMULATING_PLURAL
    fraction = _N_OF_M.match(value) and set(rel) & (ACCUMULATING | COMPLETION_WORDS)
    return bool(counted or grouped or fraction)


def is_version(value: str) -> bool:
    return bool(_VERSION.match(value.strip()))


def is_moving_version(subject: str, relation: str, value: str) -> bool:
    """A version string under a version noun called current, latest, built, installed, deployed,
    released or on main; not one a qualifier pins ("minimum version", "first released version")."""
    text = f"{subject} {relation}"
    words = set(_tokens(text))
    return (
        is_version(value)
        and bool(words & VERSION_NOUNS)
        and bool(words & MOVING or _ON_MAIN.search(text))
        and not words & QUALIFIERS
    )


def _status_value(value: str) -> bool:
    words = _tokens(value)
    return (
        bool(words)
        and set(words) <= STATUS_VALUE_WORDS | _STATUS_FILLER
        and bool(set(words) & STATUS_VALUE_WORDS)
    )


def _names_instance(subject: str) -> bool:
    words = _tokens(subject)
    return bool(_INSTANCE_ID.search(subject)) or bool(words and words[-1] in INSTANCE_NOUNS)


def is_status(subject: str, relation: str, value: str) -> bool:
    """A relation that is exactly status, state or progress (after "current" or "overall"), with
    a status word for a value or an instance for a subject. "final state", "status check",
    "review state" and "default state" are not: a qualifier, or a word before the noun."""
    if _qualified(subject, relation):
        return False
    rel = _tokens(relation)
    while rel and rel[0] in _STATUS_LEAD:
        rel = rel[1:]
    if len(rel) != 1 or rel[0] not in STATUS_RELATIONS:
        return False
    return _status_value(value) or _names_instance(subject)


def classify(subject: str, relation: str, value: str) -> str | None:
    """The volatile class of a triple (:data:`CLASSES`), or None for a durable one. Regex and
    word lists only: it runs in ``derive``'s gate and on every prompt."""
    if is_measurement(subject, relation, value):
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
        package repo repository project app cli library lib dist distribution pypi npm crate local
        checkout source tree is at on the of main master head trunk branch wheel sdist build
        published shipped
        """
    )
)
"""The words a belief about a package's own version may use besides its name. A word outside
it names something else ("memware ruff version", "memware python version")."""


def _norm_version(v: str) -> str:
    return v.strip().lower().removeprefix("v")


def manifest_rule(
    subject: str, relation: str, value: str, name: str, version: str
) -> tuple[str, str] | None:
    """(:data:`OLDER_VERSION` or :data:`CONTRADICTED`, the version read from the belief) for a
    belief about package ``name`` that the ``version`` it declares overrules, or None. The
    subject must name the package: another package's version is never compared."""
    terms = _subject_terms(name)
    if not terms or not _subject_terms(subject) & terms:
        return None
    text = f"{subject} {relation}"
    for term in sorted(terms, key=len, reverse=True):
        pattern = rf"(?<![\w.-]){re.escape(term)}[\s@_-]*v?(\d+(?:\.\d+){{1,3}})\b"
        for m in re.finditer(pattern, text, re.I):
            if older_version(m.group(1), version):
                return OLDER_VERSION, m.group(1)
    rest = _words(text) - _words(name)
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
        """The declared version to check a belief about ``subject`` against: only one whose
        package name the subject names. A subject naming no declaring package, or a package that
        declares no version (a computed one, a placeholder), is compared with nothing."""
        terms = _subject_terms(subject)
        named = [d for d in self.declared if d.name and _subject_terms(d.name) & terms]
        return named[0] if named else None

    def verdict(self, row: Any) -> Verdict | None:
        """Why ``row`` (a belief row) is left out, or None to inject it."""
        if row_human_stated(row):
            return None
        subject, relation, value = str(row["subject"]), str(row["relation"]), str(row["value"])
        declared = self.declared_for(subject)
        if declared is not None:
            ruled = manifest_rule(subject, relation, value, declared.name, declared.version)
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
