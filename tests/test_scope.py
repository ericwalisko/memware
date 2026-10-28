"""A belief from another project's world stays out of the prompt hook (memware.scope).

Two synthetic projects share one store. ``here`` talks about deploys, caches and decisions; it
never mentions the words ``elsewhere``'s beliefs are about. A belief derived in ``elsewhere``
whose subject shares only an ordinary word of ``here`` with the prompt must not be injected into
a session in ``here``; one the prompt reaches through a word ``here`` seldom uses, or through two
words, still is. Same-project, linked-worktree, person-stated, other-harness and untraceable
beliefs keep today's behaviour, and so does a project too small to judge. The same-project and
worktree beliefs use words ``here`` never does, so only their origin lets them through.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from memware import scope
from memware.cli import main
from memware.derive import source_pointer
from memware.digest import project_dir_name
from memware.ledger import assert_belief
from memware.store import Store
from tests.conftest import write_claude_jsonl

HERE_TURNS = 400
FILLER = "deploy cache retry parser branch lint queue token app location".split()

FOREIGN = ("treatment regimen decision", "chosen", "plan B")
APP = ("widgetapp app location", "is", "~/code/widgetapp")
DELIBERATE = ("rollout vendor contract", "signed", "in March")
SAME = ("ledger regimen decision", "made", "in a session here")
WORKTREE = ("dosage chart decision", "made", "in a linked worktree")
PERSON = ("treatment protocol decision", "noted", "by hand")
HERMES = ("treatment schedule decision", "noted", "in a Hermes chat")
NUMBERS = ("decision 2026-09-28", "outcome", "deferred")
UNTRACEABLE = ("treatment budget decision", "set", "from a pruned session")

DECISION = "bring me a questionnaire for the decision"


def _subject(t: tuple[str, str, str]) -> str:
    return t[0]


@pytest.fixture()
def world(tmp_path: Path, monkeypatch) -> dict[str, object]:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    projects = tmp_path / "claude" / "projects"
    here, elsewhere, tiny = (tmp_path / n for n in ("here", "elsewhere", "tiny"))
    worktree = tmp_path / "wt" / "here-fix"
    for d in (elsewhere, tiny, worktree):
        d.mkdir(parents=True)
    # here is a git repository with one live linked worktree, read from git's own files
    admin = here / ".git" / "worktrees" / "here-fix"
    admin.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {admin}\n")
    (admin / "gitdir").write_text(f"{worktree / '.git'}\n")

    def session(cwd: Path, sid: str, texts: list[str]) -> None:
        folder = projects / project_dir_name(str(cwd))
        folder.mkdir(parents=True, exist_ok=True)
        turns = [
            ("user", f"2026-09-01T00:{i // 60:02d}:{i % 60:02d}Z", t) for i, t in enumerate(texts)
        ]
        write_claude_jsonl(folder / f"{sid}.jsonl", sid, turns)

    here_texts = []
    for i in range(HERE_TURNS):
        words = [FILLER[i % len(FILLER)], FILLER[(i + 4) % len(FILLER)], "memo", f"item{i:03d}"]
        if i % 10 == 0:
            words.append("decision")  # 10% of here's passages: an ordinary word here
        if i % 100 == 0:
            words.append("rollout")  # 1%: a word here seldom uses
        here_texts.append(" ".join(words))
    session(here, "s-here", here_texts)
    session(worktree, "s-wt", ["notes from the linked worktree session, nothing more"])
    session(
        elsewhere,
        "s-else",
        [
            "the treatment regimen decision was plan B",
            "widgetapp app location is ~/code/widgetapp",
            "rollout vendor contract signed in March",
        ],
    )
    session(
        tiny,
        "s-tiny",
        [f"tiny project {FILLER[i % len(FILLER)]} decision {i:03d}" for i in range(20)],
    )

    db = str(tmp_path / "m.db")
    assert main(["--db", db, "sync", str(projects)]) == 0
    with Store(db) as s:

        def turn_of(sid: str) -> int:
            return int(
                s.conn.execute("SELECT min(id) FROM turn WHERE session=?", (sid,)).fetchone()[0]
            )

        def derived(t: tuple[str, str, str], sid: str, turn: int | None = None) -> None:
            pointer = source_pointer(sid, turn if turn is not None else turn_of(sid))
            assert_belief(s, *t, valid_from="2026-09-02T00:00:00Z", source=pointer)

        for t in (FOREIGN, APP, DELIBERATE, NUMBERS):
            derived(t, "s-else")
        derived(SAME, "s-here")
        derived(WORKTREE, "s-wt")
        derived(UNTRACEABLE, "s-gone", 999_999)
        assert_belief(s, *PERSON, valid_from="2026-09-02T00:00:00Z")
        hermes = s.conn.execute(
            "INSERT INTO turn(session, seq, ts, role, text, source, harness) VALUES "
            "('h1', 0, '2026-09-01T00:00:00Z', 'user', 'treatment schedule chat', "
            "?, 'hermes')",
            (str(tmp_path / "hermes" / "h1.jsonl"),),
        ).lastrowid
        s.conn.commit()
        derived(HERMES, "h1", int(hermes or 0))
    return {"db": db, "here": here, "elsewhere": elsewhere, "tiny": tiny, "worktree": worktree}


def _subjects(lines: set[str], *beliefs: tuple[str, str, str]) -> set[str]:
    """The belief subjects among injected lines ``subject relation: value``."""
    return {_subject(b) for b in beliefs if any(line.startswith(_subject(b)) for line in lines)}


def _injected(capsys, monkeypatch, db: str, cwd: Path, prompt: str) -> set[str]:
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": str(cwd)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    assert main(["--db", db, "context", "--from-hook", "-k", "20"]) == 0
    out = capsys.readouterr().out
    if not out:
        return set()
    block = str(json.loads(out)["hookSpecificOutput"]["additionalContext"])
    return {line[2:] for line in block.splitlines()[1:]}


ALL = (FOREIGN, APP, DELIBERATE, SAME, WORKTREE, PERSON, HERMES, NUMBERS, UNTRACEABLE)


def _got(capsys, monkeypatch, world, cwd_key: str, prompt: str) -> set[str]:
    lines = _injected(capsys, monkeypatch, str(world["db"]), Path(str(world[cwd_key])), prompt)
    return _subjects(lines, *ALL)


def test_a_belief_from_another_projects_world_stays_out_on_one_ordinary_word(
    world, capsys, monkeypatch
):
    got = _got(capsys, monkeypatch, world, "here", DECISION)
    assert _subject(FOREIGN) not in got
    # every other belief the prompt reaches on "decision" keeps today's behaviour
    assert got == {_subject(b) for b in (SAME, WORKTREE, PERSON, HERMES, NUMBERS, UNTRACEABLE)}


def test_the_same_belief_reaches_its_own_project(world, capsys, monkeypatch):
    assert _subject(FOREIGN) in _got(capsys, monkeypatch, world, "elsewhere", DECISION)


def test_a_project_too_small_to_judge_keeps_todays_behaviour(world, capsys, monkeypatch):
    assert _subject(FOREIGN) in _got(capsys, monkeypatch, world, "tiny", DECISION)


@pytest.mark.parametrize(
    "prompt",
    ["open the widgetapp app", "where does widgetapp live?"],
    ids=["two-shared-words", "one-word-this-project-never-uses"],
)
def test_an_app_location_from_another_project_still_arrives_when_named(
    world, capsys, monkeypatch, prompt
):
    assert _subject(APP) in _got(capsys, monkeypatch, world, "here", prompt)


def test_a_word_this_project_seldom_uses_is_a_deliberate_reference(world, capsys, monkeypatch):
    """The word "rollout" is in 1% of here's passages, under the 2% line: the belief it reaches
    passes, though its other words never appear here."""
    got = _got(capsys, monkeypatch, world, "here", "how did the rollout go?")
    assert _subject(DELIBERATE) in got


def test_the_cli_reads_the_project_from_the_working_directory(world, capsys, monkeypatch):
    monkeypatch.chdir(Path(str(world["here"])))
    capsys.readouterr()
    assert main(["--db", str(world["db"]), "context", DECISION, "-k", "20"]) == 0
    out = capsys.readouterr().out
    assert "regimen decision chosen" not in out.replace(":", "")
    assert "ledger regimen decision" in out


def test_the_rule_reads_only_what_it_needs(world):
    """The scope counts the project's passages, and caps each word's count at what the rule
    asks: more than 2% for the shared word, any at all for the others."""
    with Store(str(world["db"])) as s:
        from memware.digest import resolve_project, transcript_dirs

        sc = scope.ProjectScope(s.conn, transcript_dirs(resolve_project(Path(str(world["here"])))))
        assert sc.passages == HERE_TURNS + 1  # here's session and the live worktree's
        assert sc.ordinary("decision") is True
        assert sc.ordinary("rollout") is False
        assert sc.absent("treatment") is True
        assert sc.absent("treatments") is True  # stem-aware: the same word
        assert sc.absent("deploy") is False


def test_a_source_that_is_not_a_session_pointer_has_no_origin():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    assert scope._origin(conn, "stated by hand, 2026-09-02") is None


def test_a_count_fts5_refuses_keeps_todays_behaviour(tmp_path):
    """No passage index to ask: the counts read as unknown, and an unknown count never leaves a
    belief out."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE turn(id INTEGER PRIMARY KEY, session TEXT, source TEXT, harness TEXT);"
        "CREATE TABLE passage(id INTEGER PRIMARY KEY, turn_id INTEGER);"
    )
    other = str(tmp_path / "claude" / "projects" / "elsewhere" / "s.jsonl")
    conn.execute("INSERT INTO turn VALUES (1, 's', ?, 'claude-code')", (other,))
    here = tmp_path / "claude" / "projects" / "here"
    for i in range(scope.MIN_PASSAGES):
        conn.execute(
            "INSERT INTO turn VALUES (?, 'h', ?, 'claude-code')", (i + 2, f"{here}/h.jsonl")
        )
        conn.execute("INSERT INTO passage VALUES (?, ?)", (i + 1, i + 2))
    sc = scope.ProjectScope(conn, [here])
    assert sc.passages == scope.MIN_PASSAGES
    assert sc.ordinary("decision") is None and sc.absent("treatment") is None
    row = {
        "subject": "treatment regimen decision",
        "source": source_pointer("s", 1),
        "reliability": 0.5,
        "confirmed": 0,
    }
    assert scope.foreign(sc, row, {"decision"}) is False
