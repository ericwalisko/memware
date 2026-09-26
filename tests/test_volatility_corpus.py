"""The labeled volatility corpus: ``tests/data/volatility_cases.jsonl``.

One line per triple, with the class it should get, where it came from and why. The rule the
corpus pins is precision over recall: **no durable case may be left out**. Hiding a fact someone
relied on is a new harm, while a stale belief that slips through is the old behaviour and
``memware beliefs retract ID`` removes it. A volatile case the narrow rules do not catch stays in
the corpus marked ``miss``, and its test asserts the miss, so a change that starts catching it
(or stops catching a hit) has to update the corpus in the same commit.

A case with a ``project`` is judged through the injection gate with that project's declared
versions, as the prompt hook would judge it inside that project; the rest through the gate with
no project, as anywhere else.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from memware.volatile import Declared, Gate

CORPUS = Path(__file__).parent / "data" / "volatility_cases.jsonl"
CASES = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line]


def _verdict(case: dict) -> str | None:
    project = case.get("project") or {}
    gate = Gate(
        tuple(project.get("names", ())), tuple(Declared(*d) for d in project.get("declared", ()))
    )
    row = {
        "subject": case["subject"],
        "relation": case["relation"],
        "value": case["value"],
        "valid_from": "2026-09-01T00:00:00Z",
        "reliability": 0.5,
        "source": "memware:session/s/turn/1",
    }
    v = gate.verdict(row)
    return None if v is None else v.reason


def _id(case: dict) -> str:
    where = " @project" if case.get("project") else ""
    return f"{case['subject']} | {case['relation']} | {case['value'][:24]}{where}"


@pytest.mark.parametrize("case", [c for c in CASES if c["expect"] == "durable"], ids=_id)
def test_no_durable_case_is_left_out(case):
    assert _verdict(case) is None, case["why"]


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["expect"] != "durable" and not c.get("miss")], ids=_id
)
def test_each_volatile_hit_gets_its_class(case):
    assert _verdict(case) == case["expect"], case["why"]


@pytest.mark.parametrize("case", [c for c in CASES if c.get("miss")], ids=_id)
def test_each_expected_miss_is_still_missed(case):
    """The tradeoff, kept explicit: these are volatile, and the narrow rules inject them."""
    assert case["expect"] != "durable"
    assert _verdict(case) is None, f"now caught: move it out of the misses ({case['why']})"


def test_the_corpus_holds_what_the_reviews_and_the_hub_session_named():
    lines = {(c["subject"], c["relation"], c["value"]) for c in CASES}
    for triple in [
        ("card t_cd03d14d", "status", "review"),
        ("Card", "status", "blocked"),
        ("de-orphan card t_09736013", "status", "archived"),
        ("PR", "status", "open and green"),
        ("PR #125", "status", "opened"),
        ("PR #211", "status", "review"),
        ("graph_health scan", "status", "clean 0 for three weeks"),
        ("memware test suite", "test count", "91 tests"),
        ("built memware wheel", "version", "0.5.0"),
        ("memware main branch", "current version", "0.4.0"),
        ("api", "p99 latency slo", "200ms"),
        ("main branch", "python version", "3.12"),
        ("ruff", "line length", "100"),
        ("dynpkg 1.2.0", "fixed in", "x"),
    ]:
        assert triple in lines, triple
    assert any(c["subject"] == "memware 0.4.0" and c["relation"] == "known issue" for c in CASES)
    counts = Counter(c["expect"] if not c.get("miss") else "miss" for c in CASES)
    assert counts["durable"] >= 60 and counts["miss"] >= 1
    for c in CASES:
        assert c["why"] and c["from"], c


def test_the_corpus_holds_what_issue_42_and_card_t_571a37a3_named():
    """Each with the class the card requires; the last two are volatile and missed on purpose:
    too close to durable config for a word rule."""
    got = {
        (c["subject"], c["relation"], c["value"]): "miss" if c.get("miss") else c["expect"]
        for c in CASES
        if not c.get("project")
    }
    for triple, expect in [
        (("export job", "scheduled row count", "4,200 rows"), "durable"),
        (("export job", "row count", "4,200 rows"), "measurement"),
        (("field audit", "spec-required row count", "4,200 rows"), "durable"),
        (("field audit", "row count", "4,200 rows"), "measurement"),
        (("scheduled_export", "null rate", "41% null"), "measurement"),
        (("nightly_export", "null rate", "41% null"), "measurement"),
        (("appointment rows", "eligible and exported", "2,454 of 10,346"), "measurement"),
        (("appointment rows", "rows exported", "2,454 of 10,346"), "measurement"),
        (("rate limit", "requests", "1,000"), "durable"),
        (("max upload", "size", "20 MB"), "durable"),
        (("nightly backup cron", "runs every", "6 hours"), "durable"),
        (("memware PR #31", "ci status", "green"), "status"),
        (("Eric 17 Pro", "connection status", "connected"), "status"),
        (("sidebar", "default state", "open"), "durable"),
        (("circuit breaker", "initial state", "closed"), "durable"),
        (("order state machine", "final state", "completed"), "durable"),
        (("k8s deployment", "desired state", "running"), "durable"),
        (("github branch protection", "status checks", "test, lint"), "durable"),
        (("ci", "status check", "required"), "durable"),
        (("memware PR #31", "must-fix issue", "retract skips confirmed rows"), "status"),
        (("the release", "blocker", "notarisation"), "status"),
        (("memware sync at 50k turns", "latency", "3.7 s"), "miss"),
        (("personal-os board", "open cards count", "55"), "miss"),
    ]:
        assert got.get(triple) == expect, triple
    for relation in ("known issue", "open issue", "must-fix issue", "should-fix issue", "blocker"):
        assert any(c["relation"] == relation and c["expect"] == "status" for c in CASES), relation
    assert (
        got[
            (
                "memware 0.4.0",
                "known issue",
                "MEMWARE_NO_CAPTURE sessions get indexed and copied to the backup folder",
            )
        ]
        == "status"
    )


def test_the_corpus_holds_what_the_pr51_review_named():
    """The hub's review of PR #51 found durable facts the first rules hid; each is pinned with
    the class its decision requires. The last two are volatile and missed on purpose."""
    got = {
        (c["subject"], c["relation"], c["value"]): "miss" if c.get("miss") else c["expect"]
        for c in CASES
        if not c.get("project")
    }
    for triple, expect in [
        (("kanban card", "review state", "requires two approvals"), "durable"),
        (("release build", "release state", "tag then publish"), "durable"),
        (("backup job", "exit status", "non-zero on failure"), "durable"),
        (("sync indicator", "error state", "red"), "durable"),
        (("ci badge", "failing state", "red"), "durable"),
        (("pairware card", "approved state", "green"), "durable"),
        (("memware PR #31", "ci status", "green"), "status"),
        (("Eric 17 Pro", "connection status", "connected"), "status"),
        (("t_31080683 on personal-os board", "test status", "1 failed, 14 passed"), "status"),
        (("personal-os PR #152", "deployed status", "deployed"), "status"),
        (("required ci", "coverage", "90%"), "durable"),
        (("export-schedule", "rows", "4,200 rows"), "durable"),
        (("backup-retention", "files", "1,000 files"), "durable"),
        (("ruff-pin", "current version", "0.16.5"), "durable"),
        (("scheduled_export", "null rate", "41% null"), "measurement"),
        (("appointment rows", "eligible and exported", "2,454 of 10,346"), "measurement"),
        (("memware", "open issues", "tracked at github.com/ericwalisko/memware/issues"), "durable"),
        (("sqlite fts5", "known issue", "no infix matching (by design)"), "durable"),
        (("known issue", "workaround", "pass --no-cache"), "durable"),
        (
            (
                "memware 0.4.0",
                "known issue",
                "MEMWARE_NO_CAPTURE sessions get indexed and copied to the backup folder",
            ),
            "status",
        ),
        (
            (
                "memware belief classifier",
                "must-fix issue",
                "Config values are treated as measurements",
            ),
            "status",
        ),
        (("release gate tests", "must pass", "3 of 3"), "durable"),
        (("scheduled_user_sync", "row count", "4,200 rows"), "miss"),
        (("scheduled_test_run", "status", "failing"), "miss"),
    ]:
        assert got.get(triple) == expect, triple


def test_the_corpus_holds_what_card_t_91e28415_named():
    """Two findings both PR #51 holdouts missed, the forms of the same kind the card listed, and
    the pointer and by-design variants that must stay durable. The rest are durable facts the
    card's two blind holdouts found hidden by a draft of the rule, or by 0.9.0's plural rule: a
    plural relation is a list or a class, and a study's review finding is a fact."""
    got = {
        (c["subject"], c["relation"], c["value"]): "miss" if c.get("miss") else c["expect"]
        for c in CASES
        if not c.get("project")
    }
    for triple, expect in [
        (("recall", "open bug", "the fuzzy branch drops quoted phrases and needs a fix"), "status"),
        (("PR #88 review", "must-fix finding", "retract leaves the FTS row behind"), "status"),
        (
            ("memware sync", "open defect", "the last turn of a resumed session is dropped"),
            "status",
        ),
        (
            ("memware PR #31", "should-fix finding", "the docstring promises the wrong order"),
            "status",
        ),
        (("memware PR #31", "review finding", "beliefs --stale lists confirmed rows"), "status"),
        (("memware digest", "bug", "the header repeats on resume"), "status"),
        (("memware", "open bugs", "tracked at github.com/ericwalisko/memware/issues"), "durable"),
        (("sqlite fts5", "known bug", "no infix matching, by design"), "durable"),
        (
            (
                "code-review skill",
                "must-fix findings",
                "block merge until resolved or explicitly waived by the repo owner",
            ),
            "durable",
        ),
        (
            (
                "SmartBear/Cisco code review study",
                "review finding",
                "defect detection drops sharply when reviewing more than about 400 lines in one"
                " session",
            ),
            "durable",
        ),
        (
            ("memware", "open bug", "tracked at github.com/ericwalisko/memware/issues/88"),
            "durable",
        ),
        (
            ("release checklist", "OPEN BUGS", "a release ships only with zero open P0 or P1 bugs"),
            "durable",
        ),
        (
            (
                "hermes plugin PRs",
                "should-fix issues",
                "fixed in the same PR when under about 20 lines; otherwise filed as a follow-up"
                " card",
            ),
            "durable",
        ),
        (("memware", "open issues", "the digest header"), "miss"),
    ]:
        assert got.get(triple) == expect, triple
