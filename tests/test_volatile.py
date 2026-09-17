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
        ("rows backfilled", "3 of 5", True),  # a counted noun
        ("error rate", "2%", True),  # an ambiguous noun beside a counted one
        ("size", "4.2 million rows", True),  # ... or with the counted noun in the value
        ("retry limit", "5", False),  # a setting
        ("batch size", "500", False),  # "size" beside a setting
        ("max row count", "10000", False),
        ("port", "8443", False),  # not a measurement noun
        ("python version", "3.11", False),
        ("row count", "about half", False),  # not a bare quantity
        ("release date", "2026-09-15", False),
        # the review's config cases: a setting word, or an ambiguous noun beside nothing counted
        ("line length", "100", False),
        ("size", "20", False),
        ("page size", "50", False),
        ("sample rate", "0.1", False),
        ("worker count", "4", False),
        ("memory request", "512Mi", False),
        ("context length", "200000 tokens", False),
        ("rows per page", "50", False),
        ("user id", "1204", False),  # an identifier counts nothing
        ("issue number", "38", False),
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
        ("the deploy", "status", "a blue-green rollout", True),  # a status relation, any value
        ("memware 0.4.0", "known issue", "a real bug", True),
        ("memware", "open issues", "the digest header", True),
        ("the release", "blocker", "notarisation", True),
        ("memware", "build status", "green", True),
        ("card t_cd03d14d", "status", "review", True),
        ("de-orphan card t_09736013", "status", "archived", True),
        ("backfill", "progress", "83%", True),
        ("graph_health scan", "status", "clean 0 for three weeks", True),
        ("PR", "status", "open and green", True),
        ("PR #125", "status", "opened", True),
        ("PR #211", "status", "review", True),
        ("sidebar", "default state", "open", False),  # a setting word: config
        ("circuit breaker", "initial state", "closed", False),
        ("memware repo", "state management", "a reducer", False),  # a status word among others
        ("health check", "path", "/healthz", False),
        ("memware issue tracker", "host", "github", False),
    ],
)
def test_status(subject, relation, value, want):
    assert v.is_status(subject, relation, value) is want


@pytest.mark.parametrize(
    "subject, relation, value",
    [
        ("ruff", "line length", "100"),
        ("db connection pool", "size", "20"),
        ("api", "page size", "50"),
        ("sentry", "sample rate", "0.1"),
        ("gunicorn", "worker count", "4"),
        ("k8s pod", "memory request", "512Mi"),
        ("model", "context length", "200000 tokens"),
        ("sidebar", "default state", "open"),
        ("circuit breaker", "initial state", "closed"),
    ],
)
def test_durable_config_classifies_as_nothing(subject, relation, value):
    assert v.classify(subject, relation, value) is None


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
    assert v.human_stated(0.5, "memware:session/s/turn/1", confirmed=1)  # a person confirmed it
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
    declared = (v.Declared("memware", "0.6.1", "pyproject.toml"),)
    gate = v.Gate(("memware",), declared, volatile_days=3, now=now)
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
    assert v.Gate().verdict({**old, "confirmed": 1}) is None  # a person kept it
    # a reader holding only a hit: its mark and the window, no manifest
    assert gate.admits_hit(None, None) and gate.admits_hit("measurement", "2026-09-15T12:00:00Z")
    assert not gate.admits_hit("measurement", "2026-09-01T12:00:00Z")
    assert not v.Gate().admits_hit("status", "2026-09-16T11:59:00Z")


def test_the_gate_picks_the_version_the_subject_names():
    declared = (
        v.Declared("widgetry", "1.2.3", "pyproject.toml"),
        v.Declared("gadgetry", "0.3.0", "package.json"),
    )
    gate = v.Gate(("app", "widgetry", "gadgetry"), declared)
    assert gate.declared_for("widgetry wheel") == declared[0]
    assert gate.declared_for("gadgetry ui") == declared[1]
    assert gate.declared_for("app") is None  # two versions, and the subject names neither
    assert v.Gate(("app",), declared[:1]).declared_for("app") == declared[0]  # the only one
    row = {**_row("gadgetry", "version", "0.3.0"), **DERIVED}
    assert gate.verdict(row) is None
    assert gate.verdict({**row, "subject": "widgetry"}) == v.Verdict(
        v.CONTRADICTED, "pyproject.toml says 1.2.3"
    )


@pytest.mark.parametrize(
    "version, want",
    [
        ("0.0.0", True),
        ("0.0.0-development", True),
        ("v0.0", True),
        ("0.0.1", False),
        ("1.0.0", False),
    ],
)
def test_a_placeholder_version_is_not_a_version(version, want):
    assert v.is_placeholder(version) is want


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
        ("7d", 0.0),
        (True, 0.0),
    ],
)
def test_window_days_reads_a_number_or_nothing(raw, want):
    assert v.window_days({"inject": {"volatile_days": raw}}) == want


@pytest.mark.parametrize(
    "raw, want",
    [
        ("7", 7.0),
        ("2.5", 2.5),
        ("0", 0.0),
        (3, 3.0),
        ("7d", None),
        ("-3", None),
        (True, None),
        ("nan", None),
    ],
)
def test_parse_days_takes_a_non_negative_number_only(raw, want):
    assert v.parse_days(raw) == want


def test_the_manifest_reader(tmp_path):
    D = v.Declared
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "widgetry"\nversion = "1.2.3"\n')
    m = _manifest(tmp_path)
    assert (m.names, m.declared) == (("widgetry",), (D("widgetry", "1.2.3", "pyproject.toml"),))

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "widgetry"\ndynamic = ["version"]\n'
        '[tool.hatch.version]\npath = "src/widgetry/__init__.py"\n'
    )
    (tmp_path / "src" / "widgetry").mkdir(parents=True)
    (tmp_path / "src" / "widgetry" / "__init__.py").write_text(
        '"""doc"""\n\n__version__ = "2.0.0"\n'
    )
    assert _manifest(tmp_path).declared == (D("widgetry", "2.0.0", "src/widgetry/__init__.py"),)

    npm, cargo, broken, scm, poetry = (
        tmp_path / d for d in ("npm", "cargo", "broken", "scm", "poetry")
    )
    for d in (npm, cargo, broken, scm, poetry):
        d.mkdir()
    (npm / "package.json").write_text('{"name": "gadgetry", "version": "0.3.0"}')
    (cargo / "Cargo.toml").write_text('[package]\nname = "crabby"\nversion = "4.5.6"\n')
    (broken / "pyproject.toml").write_text("[project\nname = ")
    (scm / "pyproject.toml").write_text(
        '[project]\nname = "scmtool"\ndynamic = ["version"]\n[tool.setuptools_scm]\n'
    )
    (scm / "package.json").write_text('{"name": "scmtool-ui", "version": "0.0.0"}')
    (poetry / "pyproject.toml").write_text('[tool.poetry]\nname = "poet"\nversion = "0.0.0"\n')
    assert _manifest(npm) == type(m)(("gadgetry",), (D("gadgetry", "0.3.0", "package.json"),))
    assert _manifest(cargo) == type(m)(("crabby",), (D("crabby", "4.5.6", "Cargo.toml"),))
    assert _manifest(broken) == type(m)(())
    # a computed version and a placeholder declare nothing to check a belief against
    assert _manifest(scm) == type(m)(("scmtool", "scmtool-ui"), ())
    assert _manifest(poetry) == type(m)(("poet",), ())
    project = resolve_project(cargo)
    assert project.declared == (D("crabby", "4.5.6", "Cargo.toml"),) and "crabby" in project.names
