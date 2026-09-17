"""What the prompt hook and the session-start digest leave out (issue #38).

A derived belief closes only when a later derive supersedes it, so a row count, the version a
branch was at, or a PR's status went on reaching every session as a known fact long after it was
wrong. These pin each rule the injection gate applies, the exemption for what a person stated,
the configurable window and its default, the headers, and the cleanup commands. Everything runs
through the CLI on a synthetic store and a scratch project under the test's own tmp dir.
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memware.cli import main
from memware.derive import source_pointer
from memware.digest import project_dir_name
from memware.ledger import assert_belief, history
from memware.store import Store
from tests.conftest import write_claude_jsonl

SESSION = "s-release"
PROMPT = (
    "what version is memware at, how many tests in the memware test suite, the memware repo "
    "license, the memware #22 feature, and the memware ingest worker retry limit?"
)

STALE = [  # the four the reported digest injected, as derive wrote them
    ("built memware wheel", "version", "0.5.0", "2026-09-15T12:00:00Z"),
    ("memware main branch", "current version", "0.4.0", "2026-09-15T12:00:00Z"),
    (
        "memware 0.4.0",
        "known issue",
        "MEMWARE_NO_CAPTURE sessions get indexed and copied to the backup folder",
        "2026-09-15T12:00:00Z",
    ),
    ("memware test suite", "test count", "91 tests", "2026-09-09T12:00:00Z"),
]
LICENSE = ("memware repo", "license", "MIT", "2026-09-02T12:00:00Z")
RETRY = ("memware ingest worker", "retry limit", "5", "2026-09-16T12:00:00Z")


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _dump(db: str) -> str:
    with Store(db) as s:
        return "\n".join(s.conn.iterdump())


@pytest.fixture()
def app(tmp_path: Path, monkeypatch) -> Path:
    """A scratch project with one indexed session: the derived beliefs cite it, so none is an
    orphan that `retract --orphaned` would reach."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    root = tmp_path / "memware"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "memware"\nversion = "0.6.1"\n')
    folder = tmp_path / "claude" / "projects" / project_dir_name(str(root))
    folder.mkdir(parents=True)
    write_claude_jsonl(
        folder / f"{SESSION}.jsonl",
        SESSION,
        [
            ("user", "2026-09-15T12:00:00Z", "cut the 0.5.0 wheel and note the test count"),
            ("assistant", "2026-09-15T12:01:00Z", "built it; main is at 0.4.0, 91 tests pass"),
        ],
    )
    monkeypatch.chdir(root)
    return root


@pytest.fixture()
def db(tmp_path: Path, app: Path) -> str:
    path = str(tmp_path / "m.db")
    main(["--db", path, "sync", str(tmp_path / "claude" / "projects")])
    return path


def _derived(db: str, *triples: tuple[str, str, str, str]) -> None:
    with Store(db) as s:
        turn = int(s.conn.execute("SELECT max(id) FROM turn").fetchone()[0])
        for subject, relation, value, when in triples:
            assert_belief(
                s, subject, relation, value, valid_from=when, source=source_pointer(SESSION, turn)
            )


def _stated(db: str, *triples: tuple[str, str, str, str], **kw) -> None:
    with Store(db) as s:
        for subject, relation, value, when in triples:
            assert_belief(s, subject, relation, value, valid_from=when, **kw)


def _digest(capsys, db: str, cwd: Path | None = None) -> str:
    code, out, _ = _run(capsys, "--db", db, "digest", *(["--cwd", str(cwd)] if cwd else []))
    assert code == 0
    return out


def _context(capsys, db: str, monkeypatch=None, cwd: Path | None = None) -> str:
    if monkeypatch is None:
        code, out, _ = _run(capsys, "--db", db, "context", PROMPT)
        assert code == 0
        return out
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": PROMPT, "cwd": str(cwd)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code, out, _ = _run(capsys, "--db", db, "context", "--from-hook")
    assert code == 0
    return str(json.loads(out)["hookSpecificOutput"]["additionalContext"]) if out else ""


def _injected(text: str) -> set[str]:
    """The belief lines of a digest or context block, without their date."""
    lines = text.splitlines()
    heads = [
        i
        for i, line in enumerate(lines)
        if line.endswith(":") and re.match(r"(Known facts|(\w+ )?beliefs? about)", line, re.I)
    ]
    start = heads[-1] + 1 if heads else len(lines)
    return {re.sub(r" \((?:recorded|since) [\d-]+\)$", "", line[2:]) for line in lines[start:]}


def _line(t: tuple[str, str, str, str]) -> str:
    return f"{t[0]} {t[1]}: {t[2]}"


# ── the reported store ──────────────────────────────────────────────────────────────────────
def test_the_reported_store_injects_the_license_and_the_retry_limit_and_none_of_the_four(
    db, app, capsys, monkeypatch
):
    _derived(db, *STALE, LICENSE)
    _stated(db, RETRY)  # `memware assert` with no session source: a person said it

    digest = _digest(capsys, db)
    assert _injected(digest) == {_line(LICENSE), _line(RETRY)}
    assert digest.startswith("memware has 1 session and 2 beliefs for this project")

    for context in (_context(capsys, db), _context(capsys, db, monkeypatch, app)):
        assert _injected(context) == {_line(LICENSE), _line(RETRY)}


# ── each rule ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "triple",
    [
        ("memware test suite", "test count", "91 tests", "2026-09-09T12:00:00Z"),
        ("memware staging table", "null rate", "83%", "2026-09-09T12:00:00Z"),
        ("memware main branch", "current version", "0.7.0", "2026-09-15T12:00:00Z"),
        ("memware #22", "feature", "the no-capture fix", "2026-09-15T12:00:00Z"),
        ("memware ci", "status", "failing", "2026-09-15T12:00:00Z"),
    ],
    ids=["measurement", "percentage", "moving-version", "pr-contents", "status"],
)
def test_a_derived_measurement_moving_version_or_status_is_left_out(
    db, app, capsys, triple, tmp_path
):
    """No manifest needed: the class alone decides. The moving version here is newer than the
    manifest, so only its class can leave it out."""
    _derived(db, triple, LICENSE)
    for cwd in (app, None):
        assert _injected(_digest(capsys, db, cwd)) == {_line(LICENSE)}
    assert _injected(_context(capsys, db)) == {_line(LICENSE)}


def test_a_version_the_manifest_contradicts_is_left_out(db, app, capsys, tmp_path):
    """ "memware version" has no current/built qualifier, so it is not volatile by class: the
    manifest is what overrules it. A version of something else the project uses is not the
    project's version."""
    own = ("memware", "version", "0.5.0", "2026-09-15T12:00:00Z")
    other = ("memware", "ruff version", "0.16.5", "2026-09-15T12:00:00Z")
    _derived(db, own, other)
    assert _injected(_digest(capsys, db)) == {_line(other)}
    assert _injected(_context(capsys, db)) == {_line(other)}

    (app / "pyproject.toml").write_text('[project]\nname = "memware"\nversion = "0.5.0"\n')
    assert _injected(_context(capsys, db)) == {_line(own), _line(other)}


def test_a_dynamic_version_is_read_where_hatch_keeps_it(db, app, capsys):
    """memware's own pyproject declares no version: hatch reads ``__version__`` from a file."""
    (app / "pyproject.toml").write_text(
        '[project]\nname = "memware"\ndynamic = ["version"]\n'
        '[tool.hatch.version]\npath = "src/memware/__init__.py"\n'
    )
    (app / "src" / "memware").mkdir(parents=True)
    (app / "src" / "memware" / "__init__.py").write_text('__version__ = "0.6.1"\n')
    _derived(db, ("memware", "version", "0.5.0", "2026-09-15T12:00:00Z"), LICENSE)
    assert _injected(_context(capsys, db)) == {_line(LICENSE)}


def test_an_older_version_named_beside_the_project_is_history(db, app, capsys):
    old = STALE[2]
    same = ("memware 0.6.1", "known issue", "digest header claims too much", "2026-09-16T00:00:00Z")
    _derived(db, old, same)
    assert _injected(_digest(capsys, db)) == {_line(same)}
    assert _injected(_context(capsys, db)) == {_line(same)}

    elsewhere = app.parent / "elsewhere"  # no manifest: nothing to compare against
    elsewhere.mkdir()
    code, out, _ = _run(capsys, "--db", db, "beliefs", "--stale", "--cwd", str(elsewhere), "--json")
    assert code == 0 and json.loads(out) == []


@pytest.mark.parametrize(
    "how",
    [{"reliability": 0.9, "session_source": True}, {"source": "a person, in chat"}, {}],
    ids=["above-derive-reliability", "free-text-source", "no-source"],
)
def test_a_person_stating_a_fact_is_exempt_from_all_of_it(db, app, capsys, how):
    with Store(db) as s:
        turn = int(s.conn.execute("SELECT max(id) FROM turn").fetchone()[0])
    kw = dict(how)
    if kw.pop("session_source", False):
        kw["source"] = source_pointer(SESSION, turn)
    _stated(db, *STALE, **kw)
    want = {_line(t) for t in STALE}
    assert _injected(_digest(capsys, db)) == want
    assert _injected(_context(capsys, db)) == want
    code, out, _ = _run(capsys, "--db", db, "beliefs", "--stale", "--json")
    assert code == 0 and json.loads(out) == []


def test_the_window_is_configurable_and_its_default_injects_none(db, app, capsys):
    now = datetime.now(UTC)
    fresh = ("memware test suite", "test count", "230 tests", (now - timedelta(days=2)).isoformat())
    old = ("memware ingest queue", "row count", "4,200", (now - timedelta(days=10)).isoformat())
    wrong = (
        "memware main branch",
        "current version",
        "0.4.0",
        (now - timedelta(hours=1)).isoformat(),
    )
    _derived(db, fresh, old, wrong, LICENSE)

    code, out, _ = _run(capsys, "--db", db, "config", "inject.volatile_days")
    assert code == 0 and out.strip() == '{"inject.volatile_days": 0}'  # the default
    assert _injected(_context(capsys, db)) == {_line(LICENSE)}

    assert main(["--db", db, "config", "inject.volatile_days", "7"]) == 0
    got = _injected(_context(capsys, db))
    # the window lets a young measurement back in; the manifest still overrules the version
    assert got == {_line(LICENSE), _line(fresh)}
    assert _injected(_digest(capsys, db)) == got


def test_the_headers_claim_no_more_than_the_ledger_knows(db, app, capsys, monkeypatch):
    _derived(db, LICENSE)
    context, digest = _context(capsys, db, monkeypatch, app), _digest(capsys, db)
    for block in (context, digest):
        assert "currently valid" not in block.lower() and "Current beliefs" not in block
        assert "from your memory ledger" in block and "(recorded 2026-09-02)" in block
    assert context.startswith("Known facts")  # eval/recall_election's probe keys on these words


# ── seeing and cleaning up what is left out ─────────────────────────────────────────────────
def test_beliefs_stale_lists_all_four_with_a_reason_each(db, app, capsys):
    _derived(db, *STALE, LICENSE)
    _stated(db, RETRY)
    code, out, _ = _run(capsys, "--db", db, "beliefs", "--stale", "--json")
    assert code == 0
    rows = {(r["subject"], r["relation"]): (r["reason"], r["why"]) for r in json.loads(out)}
    assert rows == {
        ("built memware wheel", "version"): ("contradicted", "pyproject.toml says 0.6.1"),
        ("memware main branch", "current version"): ("contradicted", "pyproject.toml says 0.6.1"),
        ("memware 0.4.0", "known issue"): (
            "older_version",
            "names 0.4.0, pyproject.toml says 0.6.1",
        ),
        ("memware test suite", "test count"): ("measurement", "a quantity measured once"),
    }
    code, out, _ = _run(capsys, "--db", db, "beliefs", "--stale")
    assert code == 0 and out.count("left out : ") == 4


def test_the_mark_reaches_beliefs_and_recall(db, app, capsys):
    _derived(db, STALE[3], LICENSE)
    code, out, _ = _run(capsys, "--db", db, "beliefs", "--json")
    assert {r["value"]: r["volatile"] for r in json.loads(out)} == {
        "91 tests": "measurement",
        "MIT": None,
    }
    code, out, _ = _run(
        capsys, "--db", db, "recall", "memware test suite", "--what", "beliefs", "--json"
    )
    assert {h["subject"]: h["volatile"] for h in json.loads(out)} == {
        "memware test suite": "measurement",
        "memware repo": None,
    }


def test_retract_stale_is_a_dry_run_until_apply_and_keeps_every_row(db, app, capsys):
    older = ("memware main branch", "current version", "0.3.0", "2026-09-10T12:00:00Z")
    _derived(db, older, *STALE, LICENSE)
    before = _dump(db)
    code, out, err = _run(capsys, "--db", db, "beliefs", "retract", "--stale")
    assert code == 0 and "dry run" in err and "beliefs to retract : 4" in out
    assert "sessions with no turn left" not in out
    assert _dump(db) == before

    code, out, _ = _run(capsys, "--db", db, "beliefs", "retract", "--stale", "--apply", "--json")
    assert code == 0 and json.loads(out)["applied"] is True
    with Store(db) as s:
        assert s.conn.execute("SELECT count(*) FROM belief").fetchone()[0] == 6  # nothing deleted
        reasons = [r[0] for r in s.conn.execute("SELECT reason FROM retraction ORDER BY belief_id")]
        timeline = history(s, "memware main branch", "current version")
    assert len(reasons) == 4 and all("memware beliefs retract --stale" in r for r in reasons)
    assert "left out of injection, measurement: a quantity measured once" in reasons[-1]
    # the retracted version's predecessor is older still: it stays closed, not reopened
    assert [(b["value"], b["status"], b["valid_to"] is None) for b in timeline] == [
        ("0.3.0", "committed", False),
        ("0.4.0", "retracted", False),
    ]
    code, out, _ = _run(capsys, "--db", db, "beliefs", "--stale", "--json")
    assert json.loads(out) == []
    assert _injected(_digest(capsys, db)) == {_line(LICENSE)}


def test_retract_takes_belief_ids(db, app, capsys):
    _derived(db, *STALE, LICENSE)
    with Store(db) as s:
        ids = {r["value"]: r["id"] for r in s.conn.execute("SELECT id, value FROM belief")}
    before = _dump(db)
    code, _, err = _run(capsys, "--db", db, "beliefs", "retract", str(ids["91 tests"]), "999")
    assert code == 2 and "no committed belief with id 999" in err
    code, _, err = _run(capsys, "--db", db, "beliefs", "retract", str(ids["MIT"]), "--stale")
    assert code == 2 and "takes one of them" in err
    code, out, err = _run(capsys, "--db", db, "beliefs", "retract", str(ids["91 tests"]))
    assert code == 0 and "dry run" in err and "beliefs to retract : 1" in out
    assert _dump(db) == before

    assert main(["--db", db, "beliefs", "retract", str(ids["MIT"]), "--apply"]) == 0
    code, out, _ = _run(capsys, "--db", db, "beliefs", "--json")
    assert "MIT" not in {r["value"] for r in json.loads(out)}
    with Store(db) as s:
        (reason,) = s.conn.execute("SELECT reason FROM retraction").fetchone()
    assert reason == f"retracted by id (memware beliefs retract {ids['MIT']})"


def test_retracting_a_superseded_belief_by_id_relinks_and_reopens_nothing(db, app, capsys):
    """A wrong middle value: its predecessor now closes where the surviving successor starts.
    Retracting the current value reopens nothing, because what it superseded is older still."""
    first = ("memware ingest queue", "backend", "sqlite", "2026-09-01T00:00:00Z")
    middle = ("memware ingest queue", "backend", "redis", "2026-09-05T00:00:00Z")
    last = ("memware ingest queue", "backend", "postgres", "2026-09-10T00:00:00Z")
    _derived(db, first, middle, last)
    with Store(db) as s:
        ids = {r["value"]: r["id"] for r in s.conn.execute("SELECT id, value FROM belief")}
    code, out, _ = _run(capsys, "--db", db, "beliefs", "retract", str(ids["redis"]), "--apply")
    assert code == 0 and "predecessors relinked : 1" in out
    code, out, _ = _run(capsys, "--db", db, "beliefs", "retract", str(ids["postgres"]), "--apply")
    assert code == 0 and "predecessors relinked : 0" in out
    with Store(db) as s:
        timeline = history(s, "memware ingest queue", "backend")
    assert [(b["value"], b["status"], b["valid_to"]) for b in timeline] == [
        ("sqlite", "committed", "2026-09-10T00:00:00Z"),
        ("redis", "retracted", "2026-09-05T00:00:00Z"),
        ("postgres", "retracted", "2026-09-10T00:00:00Z"),
    ]


def test_stats_counts_what_injection_leaves_out_by_reason(db, app, capsys):
    _derived(db, *STALE, LICENSE)
    _stated(db, RETRY)
    code, out, _ = _run(capsys, "--db", db, "stats", "--json")
    assert code == 0
    assert json.loads(out)["injection"] == {
        "volatile_days": 0.0,
        "manifest": "pyproject.toml",
        "manifest_version": "0.6.1",
        "left_out": {
            "contradicted": 2,
            "older_version": 1,
            "measurement": 1,
            "moving_version": 0,
            "status": 0,
        },
    }
    code, out, _ = _run(capsys, "--db", db, "stats")
    assert (
        "beliefs left out of injection : 4 (2 contradicted, 1 older version, 1 measurement;" in out
    )


def _notice(monkeypatch, capsys, db: str, cwd: Path) -> str:
    payload = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(cwd)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code, out, err = _run(capsys, "--db", db, "notice", "--from-hook")
    assert code == 0 and err == ""
    return str(json.loads(out)["systemMessage"]) if out else ""


def test_an_upgrading_user_is_told_once(db, app, capsys, monkeypatch):
    main(["--db", db, "config", "setup.completed_version", "0.6.1"])  # no consent hint pending
    _derived(db, *STALE, LICENSE)
    msg = _notice(monkeypatch, capsys, db, app)
    assert msg.startswith("memware no longer injects 4 beliefs")
    assert "`memware beliefs --stale` lists them" in msg
    assert _notice(monkeypatch, capsys, db, app) == ""  # once


def test_the_notice_never_creates_a_store(tmp_path, app, capsys, monkeypatch):
    main(["--db", str(tmp_path / "x.db"), "config", "setup.completed_version", "0.6.1"])
    missing = tmp_path / "never.db"
    assert _notice(monkeypatch, capsys, str(missing), app) == ""
    assert not missing.exists()
