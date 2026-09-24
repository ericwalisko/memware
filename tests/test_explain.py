"""``memware beliefs --explain`` (issue #43): why one belief is, or is not, injected.

``--stale`` says why a belief is left out; nothing said why one is not, so a stale belief still
arriving on every prompt could only be diagnosed by driving the classifier's private predicates
by hand. ``--explain`` prints the class or durable, the test that decided it, the qualifier
behind a veto and where it came from, the exemptions and the manifest check, and whether the
belief is injected. It reads :meth:`memware.volatile.Gate.explain`, the function ``--stale``,
the prompt hook and the digest go through too. Every test runs on a synthetic store under the
test's tmp dir, with a scratch home whose ``transcript_src`` is a scratch directory.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memware.cli import main
from memware.ledger import assert_belief
from memware.store import Store

DERIVED = "memware:session/s1/turn/1"
BELIEFS = [  # (subject, relation, value, reliability, source)
    ("export job", "scheduled row count", "4,200 rows", 0.5, DERIVED),  # 1: vetoed
    ("scheduled_export", "null rate", "41% null", 0.5, DERIVED),  # 2: caught
    ("memware PR #31", "ci status", "green", 0.9, "eric, in chat"),  # 3: exempted
    ("memware", "version", "0.5.0", 0.5, DERIVED),  # 4: the manifest overrules it
    ("memware repo", "license", "MIT", 0.5, DERIVED),  # 5: durable, nothing fires
]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch memware home whose config points transcript_src at a scratch directory, so
    nothing here can walk the real transcript tree."""
    root = tmp_path / "memware-home"
    root.mkdir(exist_ok=True)
    (tmp_path / "transcripts").mkdir()
    (root / "config.json").write_text(json.dumps({"transcript_src": str(tmp_path / "transcripts")}))
    monkeypatch.setenv("MEMWARE_HOME", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project = tmp_path / "memware"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "memware"\nversion = "0.6.1"\n')
    monkeypatch.chdir(project)
    return root


@pytest.fixture()
def db(tmp_path: Path, home: Path) -> str:
    path = str(tmp_path / "m.db")
    with Store(path) as s:
        for subject, relation, value, reliability, source in BELIEFS:
            assert_belief(
                s,
                subject,
                relation,
                value,
                valid_from="2026-09-01T00:00:00Z",
                source=source,
                reliability=reliability,
            )
    return path


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _explain(capsys, db: str, *args: str) -> dict:
    code, out, err = _run(capsys, "--db", db, "beliefs", "--explain", *args, "--json")
    assert code == 0, err
    return json.loads(out)


def _fields(text: str) -> dict[str, str]:
    return {
        k.strip(): v.strip()
        for k, _, v in (line.partition(" : ") for line in text.splitlines())
        if v
    }


def test_a_vetoed_belief_names_the_qualifier_and_where_it_came_from(db, capsys):
    code, out, _ = _run(capsys, "--db", db, "beliefs", "--explain", "1")
    f = _fields(out)
    assert code == 0
    assert f["class"] == "durable" and f["injected"] == "yes"
    assert f["why"] == "durable: qualifier 'scheduled' in the relation vetoes a measurement"
    assert f["is a measurement"].startswith("no: a quantity, not an exact measure, but")
    assert f["is a status"] == "no: the relation does not end in status or state"
    assert f["reliability above 0.5"].startswith("no: 0.5")
    assert f["not a session pointer"].startswith(f"no: {DERIVED}")
    assert f["confirmed by a person"] == "no: no person has confirmed it"
    r = _explain(capsys, db, "1")
    assert r["tests"][0]["veto"] == {"token": "scheduled", "where": "relation", "within": ""}


def test_a_caught_belief_names_the_rule_that_fired(db, capsys):
    r = _explain(capsys, db, "2")
    assert r["class"] == "measurement" and r["injected"] is False
    assert r["why"] == "measurement: the relation is exactly 'null rate'"
    assert r["left_out"] == {"reason": "measurement", "detail": "a quantity measured once"}
    assert [t["fired"] for t in r["tests"]] == [True, False, False]
    assert r["checks"][-1] == {
        "name": "window",
        "applies": False,
        "detail": "inject.volatile_days is 0: never injected",
    }


def test_an_exempted_belief_says_which_exemption_and_what_it_reads_as(db, capsys):
    r = _explain(capsys, db, "3")
    assert r["class"] == "status" and r["injected"] is True and r["left_out"] is None
    assert r["why"].startswith("a person's belief, never left out (0.9, above the 0.5")
    assert r["why"].endswith("; it reads as a status")
    checks = {c["name"]: c["applies"] for c in r["checks"]}
    assert checks == {
        "reliability": True,
        "source": True,
        "confirmed": False,
        "manifest": False,
        "window": False,
    }


def test_the_manifest_check_is_shown_and_can_decide(db, capsys):
    r = _explain(capsys, db, "4")
    assert r["class"] == "durable" and r["injected"] is False
    assert r["why"] == "contradicted: pyproject.toml says 0.6.1"
    assert r["checks"][3] == {
        "name": "manifest",
        "applies": True,
        "detail": "contradicted: pyproject.toml says 0.6.1",
    }
    elsewhere = Path.cwd().parent / "elsewhere"
    elsewhere.mkdir()
    r = _explain(capsys, db, "4", "--cwd", str(elsewhere))
    assert r["injected"] is True
    assert r["checks"][3]["detail"] == "no manifest here declares a version"


def test_a_durable_belief_with_no_veto_lists_each_failed_test(db, capsys):
    r = _explain(capsys, db, "5")
    assert r["why"] == "durable: no rule fired" and r["injected"] is True
    assert [t["because"] for t in r["tests"]] == [
        "the value is not a bare quantity",
        "the value is not a version",
        "the relation does not end in status or state",
    ]


def test_the_window_can_let_a_young_volatile_belief_in(db, home, capsys):
    (home / "config.json").write_text(
        json.dumps(
            {**json.loads((home / "config.json").read_text()), "inject": {"volatile_days": 100000}}
        )
    )
    r = _explain(capsys, db, "2")
    assert r["injected"] is True and r["left_out"] is None
    assert r["checks"][-1]["applies"] is True
    assert "inside it: inject.volatile_days is 100000" in r["checks"][-1]["detail"]
    assert r["why"].startswith("measurement: the relation is exactly 'null rate'; but ")


def test_explain_agrees_with_stale_on_every_belief(db, capsys):
    code, out, _ = _run(capsys, "--db", db, "beliefs", "--stale", "--json")
    stale = {r["id"] for r in json.loads(out)}
    explained = {
        i for i in range(1, len(BELIEFS) + 1) if not _explain(capsys, db, str(i))["injected"]
    }
    assert code == 0 and stale == explained == {2, 4}


def test_a_triple_needs_no_store(tmp_path, home, capsys):
    missing = str(tmp_path / "never.db")
    code, out, err = _run(
        capsys,
        "--db",
        missing,
        "beliefs",
        "--explain",
        "--subject",
        "Eric 17 Pro",
        "--relation",
        "connection status",
        "--value",
        "connected",
    )
    f = _fields(out)
    assert code == 0, err
    assert "id" not in f and f["in ledger"].startswith("no; judged as derive would file it")
    assert f["class"] == "status" and f["injected"] == "no"
    assert f["why"] == "status: 'connection status' with a status word for a value"
    assert not Path(missing).exists()


def test_explain_reads_and_never_writes(db, capsys):
    before = Path(db).read_bytes()
    for i in range(1, len(BELIEFS) + 1):
        _explain(capsys, db, str(i))
    assert Path(db).read_bytes() == before


def test_a_belief_that_is_not_current_is_never_injected(db, capsys):
    code, _, err = _run(capsys, "--db", db, "beliefs", "retract", "5", "--apply")
    assert code == 0, err
    r = _explain(capsys, db, "5")
    assert r["in_ledger"] == "retracted" and r["injected"] is False
    assert r["why"] == (
        "not current (retracted), so never injected; if it were, durable: no rule fired"
    )


def test_a_superseded_belief_names_what_superseded_it(db, capsys):
    with Store(db) as s:
        later = assert_belief(
            s, "memware repo", "license", "Apache-2.0", valid_from="2026-09-10T00:00:00Z"
        )
    r = _explain(capsys, db, "5")
    assert r["in_ledger"] == f"superseded by {later.belief_id} at 2026-09-10T00:00:00Z"
    assert r["injected"] is False and r["why"].startswith("not current (superseded by")


def test_a_store_older_than_the_confirmation_table_is_still_explained(db, capsys):
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE confirmation")
    conn.commit()
    conn.close()
    r = _explain(capsys, db, "2")
    assert r["checks"][2] == {
        "name": "confirmed",
        "applies": False,
        "detail": "no person has confirmed it",
    }


def test_the_id_may_follow_the_flag_or_stand_alone(db, capsys):
    assert _explain(capsys, db, "2")["id"] == 2
    code, out, _ = _run(capsys, "--db", db, "beliefs", "2", "--explain", "--json")
    assert code == 0 and json.loads(out)["id"] == 2


@pytest.mark.parametrize(
    "argv, code, message",
    [
        (["--explain", "999"], 1, "no belief 999"),
        (["--explain", "abc"], 2, "--explain takes a belief id, not 'abc'"),
        (["--explain", "1", "--stale"], 2, "--explain takes one belief id"),
        (["--explain", "1", "--subject", "x"], 2, "--explain takes one belief id"),
        (["--explain", "--subject", "x", "--relation", "y"], 2, "all of --subject, --relation"),
        (["--explain"], 2, "all of --subject, --relation"),
        (["--subject", "x", "--relation", "y", "--value", "z"], 2, "go with --explain"),
        (["api", "--explain", "1"], 2, "--explain takes one belief id"),
    ],
)
def test_a_malformed_explain_is_refused(db, capsys, argv, code, message):
    got, _, err = _run(capsys, "--db", db, "beliefs", *argv)
    assert got == code and message in err
