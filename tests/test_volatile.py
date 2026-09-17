"""``memware.volatile``: the one classification derive's gate and the injection gate share.

Regex and word lists, no model, because the prompt hook runs it on every prompt. These pin the
three classes on the cases that decide them, the manifest rules, the exemption for a person, the
window arithmetic, and the manifest reader the project resolution feeds them.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memware import volatile as v
from memware.digest import _manifest, resolve_project

DERIVED = {"reliability": 0.5, "source": "memware:session/s/turn/1"}


def _row(subject, relation, value, valid_from="2026-09-15T12:00:00Z", **kw):
    return {"subject": subject, "relation": relation, "value": value, "valid_from": valid_from}


@pytest.mark.parametrize(
    "relation, value, want",
    [
        ("row count", "4.2 million rows", True),
        ("row count", "4.2 million", True),
        ("duplicate rows removed", "10,671", True),
        ("null rate", "83%", True),
        ("test count", "91 tests", True),
        ("checks passed", "3 of 5", True),
        ("p95 latency", "340ms", True),
        ("disk usage", "1.2 GB", True),
        ("retry limit", "5", False),  # a setting
        ("batch size", "500", False),  # "size" beside a setting
        ("max row count", "10000", False),
        ("port", "8443", False),  # not a measurement noun
        ("python version", "3.11", False),
        ("row count", "about half", False),  # not a bare quantity
        ("release date", "2026-09-15", False),
    ],
)
def test_measurement(relation, value, want):
    assert v.is_measurement(relation, value) is want


@pytest.mark.parametrize(
    "subject, relation, value, want",
    [
        ("built memware wheel", "version", "0.5.0", True),
        ("memware main branch", "current version", "0.4.0", True),
        ("the api", "latest release", "v2.1.0", True),
        ("ruff", "pinned version", "0.16.5", False),
        ("python", "minimum version", "3.11", False),
        ("memware", "version", "0.6.1", False),  # no qualifier: the manifest rule's business
        ("memware main branch", "current version", "abc123", False),  # not a version string
    ],
)
def test_moving_version(subject, relation, value, want):
    assert v.is_moving_version(subject, relation, value) is want


@pytest.mark.parametrize(
    "subject, relation, value, want",
    [
        ("memware PR #12", "state", "merged", True),
        ("memware #22", "feature", "the no-capture fix", True),  # what a PR contains
        ("issue 38", "root cause", "derive files measurements", True),
        ("memware ci", "status", "Failing.", True),
        ("the deploy", "status", "a blue-green rollout", False),  # not a status word
        ("memware 0.4.0", "known issue", "a real bug", False),  # "issue" with no number
        ("memware issue tracker", "host", "github", False),
    ],
)
def test_status(subject, relation, value, want):
    assert v.is_status(subject, relation, value) is want


def test_classify_names_one_class_or_none():
    assert v.classify("memware test suite", "test count", "91 tests") == v.MEASUREMENT
    assert v.classify("memware main branch", "current version", "0.4.0") == v.MOVING_VERSION
    assert v.classify("memware #22", "feature", "the no-capture fix") == v.STATUS
    assert v.classify("memware repo", "license", "MIT") is None
    assert v.classify("memware release automation", "publishes to", "PyPI") is None


def test_a_person_is_anyone_derive_is_not():
    assert not v.human_stated(0.5, "memware:session/s/turn/1")
    assert v.human_stated(0.9, "memware:session/s/turn/1")
    assert v.human_stated(0.5, "eric, in chat")
    assert v.human_stated(0.5, None)
    assert v.volatility({**_row("memware test suite", "test count", "91 tests"), **DERIVED}) == (
        v.MEASUREMENT
    )
    assert (
        v.volatility(
            {
                **_row("memware test suite", "test count", "91 tests"),
                "reliability": 0.9,
                "source": None,
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "subject, relation, value, want",
    [
        ("memware 0.4.0", "known issue", "no-capture leaks", (v.OLDER_VERSION, "0.4.0")),
        ("memware", "v0.5 known issue", "no-capture leaks", (v.OLDER_VERSION, "0.5")),  # as read
        ("memware", "known issue since 0.5", "no-capture leaks", None),  # not beside the name
        ("memware@0.5.2", "regression", "context hook slow", (v.OLDER_VERSION, "0.5.2")),
        ("memware 0.6.1", "known issue", "header claims too much", None),  # not older
        ("memware 0.10.0", "planned feature", "scan", None),  # newer, as a version not a string
        ("memware", "version", "0.5.0", (v.CONTRADICTED, "0.5.0")),
        ("memware", "version", "v0.6.1", None),  # the manifest's own
        ("built memware wheel", "version", "0.5.0", (v.CONTRADICTED, "0.5.0")),
        ("memware", "ruff version", "0.16.5", None),  # a version of something else
        ("memware", "python version", "3.11", None),
        ("ruff", "version", "0.16.5", None),  # not about the project
    ],
)
def test_manifest_rule(subject, relation, value, want):
    assert (
        v.manifest_rule(subject, relation, value, ("memware", "belief-freshness"), "0.6.1") == want
    )


def test_older_version_compares_as_versions():
    assert v.older_version("0.4.0", "0.6.1") is True
    assert v.older_version("0.10.0", "0.6.1") is False
    assert v.older_version("0.6", "0.6.0") is False
    assert v.older_version("not a version", "0.6.1") is None


def test_the_gate_orders_its_reasons_and_honours_the_window():
    now = datetime(2026, 9, 16, 12, tzinfo=UTC)
    gate = v.Gate(("memware",), "0.6.1", "pyproject.toml", volatile_days=3, now=now)
    young = {
        **_row("memware test suite", "test count", "230 tests", "2026-09-15T12:00:00Z"),
        **DERIVED,
    }
    old = {**young, "valid_from": "2026-09-01T12:00:00Z"}
    wrong = {
        **_row("memware main branch", "current version", "0.4.0", "2026-09-16T11:00:00Z"),
        **DERIVED,
    }
    assert gate.verdict(young) is None  # inside the window
    assert gate.verdict(old) == v.Verdict(v.MEASUREMENT, "a quantity measured once")
    # the manifest overrules a version however young it is
    assert gate.verdict(wrong) == v.Verdict(v.CONTRADICTED, "pyproject.toml says 0.6.1")
    assert str(gate.verdict(wrong)) == "contradicted: pyproject.toml says 0.6.1"
    assert v.Gate().verdict(old) == v.Verdict(v.MEASUREMENT, "a quantity measured once")


@pytest.mark.parametrize(
    "raw, want",
    [
        (None, 0.0),
        (0, 0.0),
        (7, 7.0),
        ("7", 7.0),
        ("1.5", 1.5),
        (-3, 0.0),
        ("soon", 0.0),
        (True, 0.0),
    ],
)
def test_window_days_reads_a_number_or_nothing(raw, want):
    assert v.window_days({"inject": {"volatile_days": raw}}) == want


def test_the_manifest_reader(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "widgetry"\nversion = "1.2.3"\n')
    assert _manifest(tmp_path) == _manifest(tmp_path)
    m = _manifest(tmp_path)
    assert (m.names, m.version, m.path) == (("widgetry",), "1.2.3", "pyproject.toml")

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "widgetry"\ndynamic = ["version"]\n'
        '[tool.hatch.version]\npath = "src/widgetry/__init__.py"\n'
    )
    (tmp_path / "src" / "widgetry").mkdir(parents=True)
    (tmp_path / "src" / "widgetry" / "__init__.py").write_text(
        '"""doc"""\n\n__version__ = "2.0.0"\n'
    )
    assert _manifest(tmp_path).version == "2.0.0"
    assert _manifest(tmp_path).path == "src/widgetry/__init__.py"

    npm, cargo, broken = tmp_path / "npm", tmp_path / "cargo", tmp_path / "broken"
    for d in (npm, cargo, broken):
        d.mkdir()
    (npm / "package.json").write_text('{"name": "gadgetry", "version": "0.3.0"}')
    (cargo / "Cargo.toml").write_text('[package]\nname = "crabby"\nversion = "4.5.6"\n')
    (broken / "pyproject.toml").write_text("[project\nname = ")
    assert _manifest(npm) == type(m)(("gadgetry",), "0.3.0", "package.json")
    assert _manifest(cargo) == type(m)(("crabby",), "4.5.6", "Cargo.toml")
    assert _manifest(broken) == type(m)(())
    assert resolve_project(cargo).version == "4.5.6" and "crabby" in resolve_project(cargo).names
