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

A qualifier always means durable, and it is checked before every rule: slo, sla, target,
threshold, budget, commitment, fail under, min, max, limit, default, initial, final, required,
desired, every, schedule, retention, pin, check, and words like them (:data:`QUALIFIERS`), as any
word of the relation or a whole word of the subject. An identifier in the subject
(``scheduled_export``, words joined by ``_`` or ``-``) is a name, and a qualifier inside it
counts only when the identifier names a setting (``export-schedule``, ``min_coverage``,
``max_rows``): see :func:`veto`. Past that:

* **measurement**: a bare quantity under a relation that is exactly progress, coverage or null
  rate; or one that is: a count, total or "number of" over an accumulating noun (rows, records,
  tests, files, lines, commits, duplicates, accounts, users, downloads); a magnitude word or a
  comma-grouped number of 1,000 or more beside such a noun; an "N of M" figure over one, in the
  relation or as the subject's noun ("appointment rows"), or over a completion word
  ("backfilled", "passed"). A requirement word in the relation ("must pass", "at least") makes
  the quantity a rule.
* **moving version**: a version string under a version noun that the subject or relation calls
  current, latest, built, installed, deployed, released, or on main.
* **status**: a relation that is exactly status, state or progress, whose value is a status word
  (open, merged, review, blocked, failing, connected, …) or whose subject names an instance (an
  id such as ``#12`` or ``t_cd03d14d``, or ending in run, scan, build, job, PR, issue or card);
  a relation ending in status ("ci status"), whose value is a status word or where an instance
  id is named (a noun is not enough; a compound "… state" is a design term); or a relation that
  names a finding (known or open issue, bug or defect, open finding, singular or plural; a
  must-fix or should-fix issue, bug, defect or finding; bug; blocker; a review finding of a named
  PR, card or run), unless the value points at where it is tracked, states a by-design
  limitation, a workaround or a won't-fix, or says where it was fixed.

:func:`decide` returns the class with the test each class ran, :meth:`Gate.explain` adds the
exemptions and the manifest; ``classify``, derive's gate, ``memware beliefs --stale`` and
``--explain`` all read them, so none of them can disagree.

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
    stale stuck waiting started complete completed progress connected disconnected
    """
)
_STATUS_FILLER = wordset("and but still now in not yet")
INSTANCE_NOUNS = wordset(
    "run runs scan scans build builds job jobs pr prs issue issues card cards ticket tickets mr"
)
_INSTANCE_ID = re.compile(r"#\d+\b|\bt_[0-9a-f]{6,}\b", re.I)
FINDINGS = frozenset(
    {
        *(
            f"{lead} {noun}"
            for lead in ("known", "open")
            for noun in ("issue", "issues", "bug", "bugs", "defect", "defects")
        ),
        "open finding",
        "open findings",
        *(
            f"{lead} {noun}"
            for lead in ("must fix", "should fix")
            for noun in ("issue", "bug", "defect", "finding")
        ),
        "bug",
        "blocker",
        "blockers",
    }
)
"""Relations that name a finding or a defect: true until someone fixes it. Known or open before
issue, bug or defect, and open finding, singular or plural; must-fix or should-fix before issue,
bug, defect or finding, singular only; "bug" or "blocker" alone.

Left out, because each is a durable fact as often as a defect: the plural of must-fix and
should-fix, which is how a rule names a class ("must-fix findings | block merge until
resolved"); "known finding" (a study's known findings); "bugs" or "finding" alone (where reports
go, what an audit found)."""
INSTANCE_FINDINGS = frozenset({"review finding"})
"""Relations that name a finding only when the subject names a PR, card or run ("PR #88 review |
review finding"): a study's review finding is a fact ("code review study | review finding |
defect detection drops past 400 lines")."""
_POINTER = re.compile(
    r"^\s*tracked\s+(?:at|in|on)\b"  # "tracked at github.com/…"
    r"|^\s*(?:(?:see|at|in)\s+)?(?:https?://|www\.|~/|\.{0,2}/)?[\w.@:~+-]*/[\w./@:~+%#?=&-]*\s*$"
    r"|^\s*(?:(?:see|at|in)\s+)?[\w./-]+\.(?:md|rst|txt|py|json|toml|ya?ml|html?)\s*$"
    r"|https?://|\bwww\.",
    re.I,
)
"""A value that points at where a finding lives (a URL, a path, "tracked at …"), not at the
defect: it stays true after the fix."""
_LIMITATION = re.compile(
    r"\bby design\b|\bwork[- ]?arounds?\b|\buse\b.+\binstead\b"
    r"|\bwon'?t[- ]?fix\b|\bwill not fix\b|\bnot a bug\b|\b(?:working|works) as intended\b",
    re.I,
)
"""A value stating a by-design limitation or a workaround, or that it will not be fixed: true
after any fix."""
_RESOLVED = re.compile(
    r"(?<!not )(?<!yet )(?<!n't )(?<!never )"
    r"\b(?:fixed|resolved|addressed|patched)\s+(?:in|by|on|with|since|as of)\b",
    re.I,
)
"""A value saying where or when it was fixed ("fixed in 0.5.0", "resolved by #40"): history, true
after the fix. Not "not yet fixed in main"."""
_OUTLIVES = (
    (_POINTER, "the value points at where it is tracked"),
    (_LIMITATION, "the value states a by-design limitation, a workaround or a won't-fix"),
    (_RESOLVED, "the value says where it was fixed"),
)
"""The values that keep a finding relation durable, in the order they are checked."""
_REQUIREMENT = re.compile(r"\bmust\b|\bat least\b", re.I)
"""Requirement words that are not qualifiers everywhere ("must-fix issue" is a finding), but
make a relation's quantity a rule: "must pass | 3 of 3"."""

BOUNDS = wordset(
    """
    min minimum max maximum limit limits cap ceiling floor quota threshold thresholds target
    targets budget slo sla ttl timeout timeouts default defaults
    """
)
MEASURED_NOUNS = ACCUMULATING | COUNT_WORDS | wordset("size sizes rate rates length number")
_SUBJECT_WORD = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")


class Veto(NamedTuple):
    """The qualifier that makes a triple a rule, a target or a setting, and where it was."""

    token: str
    where: str
    """``subject`` or ``relation``."""
    within: str = ""
    """The identifier it is part of, when it is one (``export-schedule``)."""

    def __str__(self) -> str:
        inside = f" (in '{self.within}')" if self.within else ""
        return f"qualifier '{self.token}' in the {self.where}{inside}"


def names_setting(relation: str) -> bool:
    """Whether the relation names a configured value ("retry limit", "worker count")."""
    return bool(set(_tokens(relation)) & QUALIFIERS)


def _identifier_qualifier(parts: list[str]) -> str | None:
    """The part that makes an identifier a setting's name, or None for a thing's name."""
    if parts[-1] in QUALIFIERS:
        return parts[-1]  # export-schedule, backup-retention, ruff-pin
    qualifiers = [p for p in parts if p in QUALIFIERS]
    if not qualifiers:
        return None
    bounds = [p for p in qualifiers if p in BOUNDS]
    if bounds:
        return bounds[0]  # min_coverage, max_upload
    if set(parts) & MEASURED_NOUNS:
        return qualifiers[0]  # max_rows, page_size, scheduled_user_sync
    return None


def veto(subject: str, relation: str) -> Veto | None:
    """The qualifier that makes a triple durable, or None. It is checked before every rule.

    Every word of the relation counts, split at ``_`` and ``-`` too: "scheduled row count" and
    "spec-required row count" are rules. A whole word of the subject counts: "rate limit", "max
    upload", "required ci" and "nightly backup cron" are settings. An identifier in the subject
    (words joined by ``_`` or ``-``) is a name, so a qualifier inside it counts only when the
    identifier names a setting: its last part is the qualifier (``export-schedule``,
    ``ruff-pin``), or it holds a bound (``min_coverage``), or it joins a qualifier to what would
    be measured (``max_rows``, ``page_size``). ``scheduled_export`` names an export."""
    for token in _tokens(relation):
        if token in QUALIFIERS:
            return Veto(token, "relation")
    for word in _SUBJECT_WORD.findall(subject.lower()):
        parts = _tokens(word)
        if len(parts) == 1:
            if word in QUALIFIERS:
                return Veto(word, "subject")
            continue
        named = _identifier_qualifier(parts)
        if named is not None:
            return Veto(named, "subject", word)
    return None


class Test(NamedTuple):
    """One class's test on a triple: whether it fired, and the rule that fired or the check that
    failed."""

    cls: str
    """One of :data:`CLASSES`."""
    fired: bool
    because: str
    veto: Veto | None = None
    outlives: str = ""
    """When the relation names a finding but the value stays true after a fix: why."""


def measurement_test(subject: str, relation: str, value: str) -> Test:
    """A quantity that no qualifier or requirement word vetoes, under a relation that is exactly
    progress, coverage or null rate, or a count, magnitude or N of M over what accumulates."""
    if not _QUANTITY.match(value.strip()):
        return Test(MEASUREMENT, False, "the value is not a bare quantity")
    v = veto(subject, relation)
    if v is not None:
        return Test(MEASUREMENT, False, f"a quantity, but {v} vetoes it", v)
    required = _REQUIREMENT.search(relation)
    if required:
        word = required.group(0).lower()
        return Test(MEASUREMENT, False, f"a quantity, but '{word}' in the relation makes it a rule")
    rel = _tokens(relation)
    joined = " ".join(rel)
    if joined in EXACT_MEASURES:
        return Test(MEASUREMENT, True, f"the relation is exactly '{joined}'")
    words = set(rel) | set(_tokens(value))
    counted_by = sorted(set(rel) & COUNT_WORDS) or (["number of"] if "number of" in joined else [])
    if counted_by and words & ACCUMULATING:
        noun = sorted(words & ACCUMULATING)[0]
        return Test(MEASUREMENT, True, f"'{counted_by[0]}' over '{noun}'")
    if (magnitude := _MAGNITUDE.search(value)) and words & ACCUMULATING_PLURAL:
        noun = sorted(words & ACCUMULATING_PLURAL)[0]
        return Test(MEASUREMENT, True, f"'{magnitude.group(0)}' beside '{noun}'")
    if _N_OF_M.match(value):
        over = sorted(set(rel) & (ACCUMULATING | COMPLETION_WORDS))
        if over:
            return Test(MEASUREMENT, True, f"an N of M over '{over[0]}' in the relation")
        head = _tokens(subject)[-1:]
        if head and head[0] in ACCUMULATING:
            return Test(MEASUREMENT, True, f"an N of M over '{head[0]}', the subject's noun")
        return Test(
            MEASUREMENT, False, "an N of M, but not over a counted noun or a completion word"
        )
    return Test(
        MEASUREMENT,
        False,
        "a quantity, but no count, magnitude or N of M over rows, tests, files or the like",
    )


def is_measurement(subject: str, relation: str, value: str) -> bool:
    """An unambiguous measurement; see the module docstring and :func:`measurement_test`."""
    return measurement_test(subject, relation, value).fired


def is_version(value: str) -> bool:
    return bool(_VERSION.match(value.strip()))


def moving_version_test(subject: str, relation: str, value: str) -> Test:
    """A version string under a version noun called current, latest, built, installed, deployed,
    released or on main; not one a qualifier pins ("minimum version", "first released version")."""
    text = f"{subject} {relation}"
    words = set(_tokens(text))
    if not is_version(value):
        return Test(MOVING_VERSION, False, "the value is not a version")
    if not words & VERSION_NOUNS:
        return Test(MOVING_VERSION, False, "no version, release or tag in the subject or relation")
    moving = sorted(words & MOVING) or (["on main"] if _ON_MAIN.search(text) else [])
    if not moving:
        return Test(
            MOVING_VERSION,
            False,
            "nothing calls it current, latest, built, installed, deployed, released or on main",
        )
    v = veto(subject, relation)
    if v is not None:
        return Test(MOVING_VERSION, False, f"a version called '{moving[0]}', but {v} vetoes it", v)
    return Test(MOVING_VERSION, True, f"a version called '{moving[0]}'")


def is_moving_version(subject: str, relation: str, value: str) -> bool:
    """See :func:`moving_version_test`."""
    return moving_version_test(subject, relation, value).fired


def _status_value(value: str) -> bool:
    words = _tokens(value)
    return (
        bool(words)
        and set(words) <= STATUS_VALUE_WORDS | _STATUS_FILLER
        and bool(set(words) & STATUS_VALUE_WORDS)
    )


def _instance(subject: str) -> str | None:
    m = _INSTANCE_ID.search(subject)
    if m:
        return m.group(0)
    words = _tokens(subject)
    return words[-1] if words and words[-1] in INSTANCE_NOUNS else None


def status_test(subject: str, relation: str, value: str) -> Test:
    """Three shapes, each vetoed by a qualifier ("default state", "status check"):

    * a relation that is exactly status, state or progress (after "current" or "overall"), with
      a status word for a value or an instance (an id, or a run, job, PR, card …) for a subject;
    * a relation ending in status ("ci status", "connection status"), with a status word for a
      value or an instance id (``#31``, ``t_31080683``) in the subject or relation: a subject
      noun is not enough ("backup job exit status: non-zero on failure" is a rule). A compound
      "… state" is a design term ("error state", "review state") and is never a status;
    * a relation naming a finding (:data:`FINDINGS`: known or open issue, bug or defect, open
      finding; a must-fix or should-fix issue, bug, defect or finding, singular only; bug;
      blocker; a review finding when the subject names a PR, card or run), unless the value
      points at where it is tracked (a URL, a path, "tracked at …"), states a by-design
      limitation, a workaround or a won't-fix, or says where it was fixed ("fixed in 0.5.0"):
      those stay true after a fix."""
    rel = _tokens(relation)
    while rel and rel[0] in _STATUS_LEAD:
        rel = rel[1:]
    joined = " ".join(rel)
    if joined in INSTANCE_FINDINGS and not _instance(subject):
        return Test(
            STATUS,
            False,
            f"'{joined}', but the subject names no PR, card or run: a study's finding is a fact",
        )
    finding = joined in FINDINGS or joined in INSTANCE_FINDINGS
    exact = len(rel) == 1 and rel[0] in STATUS_RELATIONS
    compound = len(rel) > 1 and rel[-1] == "status"
    if not (finding or exact or compound):
        return Test(
            STATUS,
            False,
            "the relation is not status, state or progress, a compound status, or a finding",
        )
    v = veto(subject, relation)
    if v is not None:
        return Test(STATUS, False, f"'{joined}', but {v} vetoes it", v)
    if finding:
        names = f"the relation names a finding, '{joined}'"
        outlives = next((why for pattern, why in _OUTLIVES if pattern.search(value)), "")
        if outlives:
            return Test(STATUS, False, f"{names}, but {outlives}", outlives=outlives)
        return Test(STATUS, True, names)
    if _status_value(value):
        return Test(STATUS, True, f"'{joined}' with a status word for a value")
    if exact:
        instance = _instance(subject)
        if instance:
            return Test(STATUS, True, f"'{joined}' of an instance, '{instance}'")
        return Test(
            STATUS,
            False,
            f"'{joined}', but the value is not a status word and no instance is named",
        )
    m = _INSTANCE_ID.search(f"{subject} {relation}")
    if m:
        return Test(STATUS, True, f"'{joined}' of an instance, '{m.group(0)}'")
    return Test(
        STATUS,
        False,
        f"'{joined}', but the value is not a status word and no instance id (#N, t_…) is named",
    )


def is_status(subject: str, relation: str, value: str) -> bool:
    """See :func:`status_test`."""
    return status_test(subject, relation, value).fired


@dataclass(frozen=True)
class Decision:
    """How a triple was classified: its class (None for durable) and each class's test, in the
    order :func:`classify` tries them. The first test that fired decided it."""

    cls: str | None
    tests: tuple[Test, ...]

    @property
    def because(self) -> str:
        """The deciding rule, or, for a durable triple, every test's failed check."""
        for t in self.tests:
            if t.fired:
                return f"{label(t.cls)}: {t.because}"
        return "; ".join(f"not a {label(t.cls)}: {t.because}" for t in self.tests)


def decide(subject: str, relation: str, value: str) -> Decision:
    """The classification of a triple with the path that decided it: what :func:`classify`
    returns, what ``derive``'s gate rejects, and what ``memware beliefs --explain`` prints."""
    tests = (
        measurement_test(subject, relation, value),
        moving_version_test(subject, relation, value),
        status_test(subject, relation, value),
    )
    fired = [t.cls for t in tests if t.fired]
    return Decision(fired[0] if fired else None, tests)


def classify(subject: str, relation: str, value: str) -> str | None:
    """The volatile class of a triple (:data:`CLASSES`), or None for a durable one. Regex and
    word lists only: it runs in ``derive``'s gate and on every prompt."""
    return decide(subject, relation, value).cls


class Check(NamedTuple):
    """One test the injection gate applies to a row besides the classes, and what it found."""

    name: str
    """``reliability``, ``source``, ``confirmed``, ``manifest`` or ``window``."""
    applies: bool
    """For the first three, whether it makes the belief a person's; for ``manifest``, whether
    the manifest overrules it; for ``window``, whether the window lets a volatile one in."""
    detail: str


def person_checks(
    reliability: object, source: object, confirmed: object = False
) -> tuple[Check, ...]:
    """The three exemptions, each with what it found: reliability above derive's, a source that
    is not a session pointer, a confirmation by a person."""
    try:
        above = float(str(reliability)) > DERIVED_RELIABILITY
    except ValueError:
        above = False
    pointer = str(source or "").startswith(DERIVED_SOURCE)
    return (
        Check(
            "reliability",
            above,
            f"{reliability}, above the {DERIVED_RELIABILITY} derive writes"
            if above
            else f"{reliability}, not above the {DERIVED_RELIABILITY} derive writes",
        ),
        Check(
            "source",
            not pointer,
            f"{source}, a session pointer: derive wrote it"
            if pointer
            else f"{source or 'none'}, not a {DERIVED_SOURCE} pointer: a person stated it",
        ),
        Check(
            "confirmed",
            bool(confirmed),
            "a person asserted the same value or approved it in review"
            if confirmed
            else "no person has confirmed it",
        ),
    )


def human_stated(reliability: object, source: object, confirmed: object = False) -> bool:
    """A person asserted it: reliability above derive's, a source that is not a session pointer
    (``remember`` and ``memware assert`` take free text, or none), or a person confirmed the
    derived belief since (``confirmed``, from the ``confirmation`` table: the same value
    asserted again, or a review approval)."""
    return any(c.applies for c in person_checks(reliability, source, confirmed))


def _row_confirmed(row: Any) -> object:
    columns = row.keys()  # a sqlite3.Row: `in` would search its values, not its column names
    return row["confirmed"] if "confirmed" in columns else False


def row_human_stated(row: Any) -> bool:
    """:func:`human_stated` for a belief row; ``confirmed`` counts when the query selected it."""
    return human_stated(row["reliability"], row["source"], _row_confirmed(row))


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

    def explain(self, row: Any) -> Explanation:
        """Every test the gate applies to ``row`` (a belief row), what each found, and the
        verdict. :meth:`verdict` is this verdict, so ``memware beliefs --stale``, ``--explain``,
        the prompt hook and the digest cannot disagree."""
        subject, relation, value = str(row["subject"]), str(row["relation"]), str(row["value"])
        decision = decide(subject, relation, value)
        person = person_checks(row["reliability"], row["source"], _row_confirmed(row))
        declared = self.declared_for(subject)
        ruled: Verdict | None = None
        if not self.declared:
            manifest = Check("manifest", False, "no manifest here declares a version")
        elif declared is None:
            names = ", ".join(sorted({d.name for d in self.declared if d.name}))
            manifest = Check("manifest", False, f"the subject names no declaring package ({names})")
        else:
            found = manifest_rule(subject, relation, value, declared.name, declared.version)
            where = f"{declared.path} says {declared.version}"
            if found is None:
                manifest = Check("manifest", False, f"{where}; the belief does not contradict it")
            else:
                reason, named = found
                detail = f"names {named}, {where}" if reason == OLDER_VERSION else where
                ruled = Verdict(reason, detail)
                manifest = Check("manifest", True, f"{label(reason)}: {detail}")
        window, young = self._window(row, decision.cls)
        if any(c.applies for c in person):
            verdict = None
        elif ruled is not None:
            verdict = ruled
        elif decision.cls is None or young:
            verdict = None
        else:
            verdict = Verdict(decision.cls, DESCRIBE[decision.cls])
        return Explanation(decision, (*person, manifest, window), verdict)

    def _window(self, row: Any, cls: str | None) -> tuple[Check, bool]:
        if cls is None:
            return Check("window", False, "durable: the window does not apply"), False
        if not self.volatile_days:
            return Check("window", False, f"{WINDOW_KEY} is 0: never injected"), False
        age = _age_days(row["valid_from"], self.now or datetime.now(UTC))
        days = f"{WINDOW_KEY} is {self.volatile_days:g}"
        if age is None:
            return Check("window", False, f"no recorded date; {days}"), False
        young = age < self.volatile_days
        inside = "inside" if young else "outside"
        return Check("window", young, f"{age:.1f} days old, {inside} it: {days}"), young

    def verdict(self, row: Any) -> Verdict | None:
        """Why ``row`` (a belief row) is left out, or None to inject it."""
        return self.explain(row).verdict

    def admits_hit(self, volatile: str | None, valid_from: str | None) -> bool:
        """For a reader that has only a recall hit (the Hermes provider): its ``volatile`` mark,
        honouring the window. No manifest rule: a hit does not say which project it is in."""
        if volatile is None:
            return True
        if not self.volatile_days:
            return False
        age = _age_days(valid_from, self.now or datetime.now(UTC))
        return age is not None and age < self.volatile_days


@dataclass(frozen=True)
class Explanation:
    """What :meth:`Gate.explain` found for one belief."""

    decision: Decision
    """The triple's classification, with each class's test."""
    checks: tuple[Check, ...]
    """reliability, source, confirmed, manifest, window: in that order."""
    verdict: Verdict | None
    """Why injection leaves it out, or None: it is injected."""

    @property
    def injected(self) -> bool:
        return self.verdict is None

    @property
    def why(self) -> str:
        """One line: what decided it."""
        person = [c for c in self.checks[:3] if c.applies]
        if person:
            cls = self.decision.cls
            reads = f"; it reads as a {label(cls)}" if cls else ""
            return f"a person's belief, never left out ({person[0].detail}){reads}"
        if self.verdict is not None and self.verdict.reason not in CLASSES:
            return str(self.verdict)
        if self.decision.cls is not None:
            window = self.checks[-1]
            rule = self.decision.because
            return f"{rule}; but {window.detail}" if window.applies else rule
        vetoed = [t for t in self.decision.tests if t.veto is not None]
        if vetoed:
            return f"durable: {vetoed[0].veto} vetoes a {label(vetoed[0].cls)}"
        outlived = [t for t in self.decision.tests if t.outlives]
        if outlived:
            return f"durable: {outlived[0].because}"
        return "durable: no rule fired"


DESCRIBE = {
    MEASUREMENT: "a quantity measured once",
    MOVING_VERSION: "what something was at when recorded",
    STATUS: "the state of an issue, PR, run or check",
}


def label(reason: str) -> str:
    """``moving_version`` -> ``moving version``: the words a person reads for a reason key."""
    return reason.replace("_", " ")
