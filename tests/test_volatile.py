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


def test_classify_names_one_class_or_none():
    """One of each; every other case, with its reason, is in tests/data/volatility_cases.jsonl."""
    assert v.classify("memware test suite", "test count", "91 tests") == v.MEASUREMENT
    assert v.classify("memware main branch", "current version", "0.4.0") == v.MOVING_VERSION
    assert v.classify("card t_cd03d14d", "status", "review") == v.STATUS
    assert v.classify("memware repo", "license", "MIT") is None
    assert v.classify("api", "p99 latency slo", "200ms") is None  # a qualifier: durable


@pytest.mark.parametrize(
    "relation", ["retry limit", "worker count", "sessions listed", "runs every", "fail under"]
)
def test_a_qualifier_names_a_setting(relation):
    assert v.names_setting(relation)


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
        ("dynpkg 1.2.0", "fixed in", "x", None),  # names another package than the declared one
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
    assert v.manifest_rule(subject, relation, value, "memware", "0.6.1") == want
    assert v.manifest_rule("dynpkg 1.2.0", "fixed in", "x", "dynpkg-ui", "1.4.0") is None


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
    assert gate.declared_for("app") is None  # the subject names no declaring package
    # not even when only one is declared: another package's version is never compared
    assert v.Gate(("app",), declared[:1]).declared_for("app") is None
    dyn = v.Gate(("dynpkg", "dynpkg-ui"), (v.Declared("dynpkg-ui", "1.4.0", "package.json"),))
    assert dyn.verdict({**_row("dynpkg 1.2.0", "fixed in", "x"), **DERIVED}) is None
    assert dyn.verdict({**_row("dynpkg-ui 1.2.0", "fixed in", "x"), **DERIVED}) == v.Verdict(
        v.OLDER_VERSION, "names 1.2.0, package.json says 1.4.0"
    )
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
