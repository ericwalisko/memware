"""What an applied prune must never do on the way to removing a value.

Pins the final check of #40: prune printed the value it removes (the retraction it cascades into
listed beliefs as they were before redaction), rewrote derive's session pointers when the text
was a word they contain, rewrote hundreds of beliefs for a word with nothing to stop it, left a
redacted candidate's review open to be approved, and let a hook's sync wait a minute for the lock.
Every store here is synthetic, under the test's own tmp dir.
"""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import pytest

import memware.cli as cli
from memware.cli import main
from memware.derive import source_pointer
from memware.ingest import prune, sync_tree
from memware.ledger import (
    REDACT_MAX_BELIEFS,
    REDACT_MIN_CHARS,
    NotApprovable,
    Policy,
    RedactionRefused,
    approve,
    assert_belief,
)
from memware.review import JsonlReviewBackend, sync_reviews
from memware.store import Store

SECRET = "ECHOSECRET99"
PREFIX = f"{SECRET} is the staging api key"


def _write(path: Path, session: str, texts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps({"role": "user", "content": t, "session": session}) + "\n" for t in texts
        )
    )


def _store(tmp_path: Path) -> Path:
    """A session that holds only the pasted value, beliefs derive filed from it, a person's belief
    that names the session and quotes the value, and a derived value it supersedes, so a prune's
    retraction lists beliefs to retract, to reopen and to keep."""
    root = tmp_path / "corpus"
    _write(root / "paste.jsonl", "paste", [f"{PREFIX} for the deploy"])
    _write(
        root / "work.jsonl",
        "work",
        ["an ordinary work turn about the deploy", "and another ordinary turn about the deploy"],
    )
    db = tmp_path / "s.db"
    with Store(db) as s:
        sync_tree(s, root, harness="generic")
        paste = s.conn.execute("SELECT id FROM turn WHERE session='paste'").fetchone()[0]
        work = s.conn.execute("SELECT id FROM turn WHERE session='work'").fetchone()[0]
        pasted, worked = source_pointer("paste", paste), source_pointer("work", work)
        assert_belief(s, "staging api key", "is", SECRET, source=pasted)
        assert_belief(s, f"{PREFIX} note", "says", "rotate it", source=pasted)
        assert_belief(s, "deploy token", "is", "old-token", source=worked, policy=Policy.AUTO)
        assert_belief(s, "deploy token", "is", f"{PREFIX} now", source=pasted, policy=Policy.AUTO)
        assert_belief(s, "pasted in", "the chat", f"{SECRET} too", reliability=0.9, source="paste")
    return db


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.parametrize("view", [[], ["--plain"], ["--json"]])
@pytest.mark.parametrize("apply", [[], ["--apply"]])
@pytest.mark.parametrize(
    ("selector", "text"),
    [("--turns-containing", SECRET), ("--containing", SECRET), ("--turns-starting-with", PREFIX)],
)
def test_prune_never_prints_the_text_it_removes(tmp_path, capsys, selector, text, apply, view):
    """Final check of #40, HIGH: a dry run or an applied prune listed a belief it retracts as
    ``value : ECHOSECRET99``. No view, mode or selector may print the text, in any case."""
    db = _store(tmp_path)
    code, out, err = _run(capsys, "--db", str(db), "prune", selector, text, *apply, *view)
    assert code == 0, err
    for form in (text, text.lower()):  # as given, and as a belief's key holds it
        assert form not in out and form not in err, (form, out, err)
    if view == ["--json"]:
        body = json.loads(out)  # still valid JSON with the text withheld
        assert body["retract"], "the retraction lists beliefs, withheld"
    else:
        assert "[removed]" in out


def test_a_redacted_subject_prints_its_key_with_the_marker_intact(tmp_path, capsys):
    """Cosmetic finding from the final check of #40: the cascade rebuilt a retracted belief's
    key from its already-withheld subject via ``make_key``, whose ``normalize`` strips leading
    and trailing punctuation, eating the opening ``[`` of a ``[removed]`` marker sitting at the
    edge of the field. It printed ``removed] is the staging api key note|says`` -- the marker
    must survive intact."""
    db = _store(tmp_path)
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", SECRET, "--json")
    assert code == 0
    body = json.loads(out)
    note = next(r for r in body["retract"] if r["relation"] == "says")
    assert note["subject"] == "[removed] is the staging api key note"
    assert note["key"] == "[removed] is the staging api key note|says"


def test_error_paths_withhold_the_text_too(tmp_path, capsys, monkeypatch):
    """An error that names the text, here a scrub failing with it in the message, is withheld."""
    db = _store(tmp_path)

    def failing_scrub(self, progress=None):
        raise sqlite3.OperationalError(f"disk I/O error near {SECRET}")

    monkeypatch.setattr(Store, "scrub", failing_scrub)
    code, out, err = _run(capsys, "--db", str(db), "prune", "--turns-containing", SECRET, "--apply")
    assert code == 1 and "the scrub did not finish" in err
    assert SECRET not in out + err and SECRET.lower() not in out + err


def test_a_session_pointer_is_never_matched_or_rewritten(tmp_path, capsys):
    """Final check of #40: pruning ``memware`` or ``session`` matched derive's
    ``memware:session/<id>/turn/<n>`` source and rewrote it to ``[removed]:session/...``, which
    loses the provenance and makes a derived belief read as a person's."""
    db = _store(tmp_path)
    with Store(db) as s:
        before = {r[0]: r[1] for r in s.conn.execute("SELECT id, source FROM belief")}
        stated = assert_belief(
            s, "notes", "kept", "fine", reliability=0.9, source="a session note"
        ).belief_id
        hermes = assert_belief(
            s, "hermes", "note", "x", reliability=0.9, source="hermes built-in memory (add)"
        ).belief_id
    for word in ("memware", "session", "turn", "memory"):
        code, out, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", word, "--json")
        redacted = json.loads(out)["beliefs_redacted"]
        assert code == 0 and not set(redacted) & (set(before) | {hermes}), (word, redacted)
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", "session", "--json")
    assert json.loads(out)["beliefs_redacted"] == [stated]  # a free-text source still matches

    _run(capsys, "--db", str(db), "prune", "--turns-containing", "session", "--apply")
    with Store(db) as s:
        after = {r[0]: r[1] for r in s.conn.execute("SELECT id, source FROM belief")}
    assert {i: after[i] for i in before} == before and after[
        hermes
    ] == "hermes built-in memory (add)"
    assert after[stated] == "a [removed] note"


def _ledger(tmp_path: Path, n: int) -> Path:
    """``n`` beliefs that name ``platform``, as a word would be named across a ledger."""
    db = _store(tmp_path)
    with Store(db) as s:
        for i in range(n):
            assert_belief(s, f"service {i}", "runs on", "the platform cluster")
    return db


def _dump(db: Path) -> str:
    with Store(db) as s:
        return "\n".join(s.conn.iterdump())


def test_a_redaction_too_broad_to_be_a_secret_is_refused_whole(tmp_path, capsys):
    db = _ledger(tmp_path, REDACT_MAX_BELIEFS + 1)
    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", "platform", "--json"
    )
    r = json.loads(out)
    assert code == 0 and len(r["beliefs_redacted"]) == REDACT_MAX_BELIEFS + 1
    assert r["redaction_refused"] == f"it would redact 21 beliefs, more than {REDACT_MAX_BELIEFS}"
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", "platform")
    assert "redaction guard : --apply refuses, because it would redact 21 beliefs" in out

    before = _dump(db)
    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", "platform", "--apply"
    )
    assert code == 2 and _dump(db) == before  # nothing written: no turn, no belief, no scrub
    assert err.startswith("refused, nothing written: it would redact 21 beliefs, more than 20.")
    assert "--allow-broad-redaction" in err and "beliefs to redact : 21" in out
    assert "platform" not in out + err

    with Store(db) as s:
        with pytest.raises(RedactionRefused):
            prune(s, turns_containing="platform", apply=True)
    assert _dump(db) == before

    code, out, _ = _run(
        capsys, "--db", str(db), "prune", "--turns-containing", "platform", "--apply",
        "--allow-broad-redaction", "--json",
    )  # fmt: skip
    r = json.loads(out)
    assert code == 0 and len(r["beliefs_redacted"]) == 21 and r["redaction_refused"]
    with Store(db) as s:
        assert (
            s.conn.execute("SELECT count(*) FROM belief WHERE instr(value, 'platform')").fetchone()[
                0
            ]
            == 0
        )


def test_a_short_text_that_would_redact_a_belief_is_refused(tmp_path, capsys):
    db = _store(tmp_path)
    short = "api"  # in the staging api key belief
    assert len(short) < REDACT_MIN_CHARS
    code, out, err = _run(capsys, "--db", str(db), "prune", "--turns-containing", short, "--apply")
    assert code == 2 and "the text is 3 characters, shorter than 6" in err

    # a short text that redacts no belief removes turns as before: the guard is about beliefs
    code, out, err = _run(
        capsys, "--db", str(db), "prune", "--turns-starting-with", "and", "--apply", "--json"
    )
    assert code == 0 and json.loads(out)["turns_removed"] == 1


def test_redaction_closes_the_review_of_a_candidate_it_rewrites(tmp_path, capsys):
    """Final check of #40: redaction left a redacted candidate's review open, `review approve`
    committed ``[removed]`` over the valid incumbent, and the prompt hook injected it."""
    db = _store(tmp_path)
    with Store(db) as s:
        assert_belief(s, "cache", "ttl", "60 seconds", reliability=0.9)
        pending = assert_belief(s, "cache", "ttl", f"{SECRET} seconds", reliability=0.2)
        assert_belief(s, "queue", "size", f"{SECRET} items", reliability=0.9)
        other = assert_belief(s, "queue", "size", "10 items", reliability=0.2)  # incumbent redacted
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", SECRET, "--json")
    assert json.loads(out)["reviews_closed_by_redaction"] == [pending.review_id, other.review_id]
    _run(capsys, "--db", str(db), "prune", "--turns-containing", SECRET, "--apply")

    with Store(db) as s:
        rows = {r["id"]: dict(r) for r in s.conn.execute("SELECT * FROM review")}
    for review_id in (pending.review_id, other.review_id):
        assert rows[review_id]["decision"] == "redacted" and rows[review_id]["decided_at"]
        assert rows[review_id]["reason"].endswith(
            "; closed: memware prune: text redacted (value withheld)"
        )
    code, out, err = _run(capsys, "--db", str(db), "review", "approve", str(pending.review_id))
    assert code == 2 and "no open review" in err
    code, out, _ = _run(capsys, "--db", str(db), "context", "what is the cache ttl")
    assert "[removed]" not in out and "60 seconds" in out


def test_approving_a_redacted_or_retracted_candidate_is_refused(tmp_path):
    db = _store(tmp_path)
    with Store(db) as s:
        assert_belief(s, "cache", "ttl", "60 seconds", reliability=0.9)
        redacted = assert_belief(s, "cache", "ttl", "90 seconds", reliability=0.2)
        s.conn.execute(
            "UPDATE belief SET value='[removed] seconds' WHERE id=?", (redacted.belief_id,)
        )
        with pytest.raises(NotApprovable, match="a prune redacted its candidate"):
            approve(s, redacted.review_id)
        assert_belief(s, "queue", "size", "10", reliability=0.9)
        retracted = assert_belief(s, "queue", "size", "12", reliability=0.2)
        s.conn.execute("UPDATE belief SET status='retracted' WHERE id=?", (retracted.belief_id,))
        with pytest.raises(NotApprovable, match="its candidate is retracted, not pending"):
            approve(s, retracted.review_id)

        inbox = tmp_path / "inbox.jsonl"
        inbox.write_text(
            json.dumps({"review_id": redacted.review_id, "decision": "approve"}) + "\n"
        )
        backend = JsonlReviewBackend(str(tmp_path / "outbox.jsonl"), str(inbox))
        assert sync_reviews(s, backend)["applied"] == 0  # a sync skips it, as a decided review
        assert (
            s.conn.execute(
                "SELECT status FROM belief WHERE id=?", (redacted.belief_id,)
            ).fetchone()[0]
            == "candidate"
        )


def test_a_hook_sync_under_a_held_lock_gives_up_quietly_and_the_next_sync_catches_up(
    tmp_path, capsys, monkeypatch
):
    """Final check of #40: `memware sync --from-hook` (PreCompact, 30 s hook timeout) waited up to
    a minute for the lock. It now waits a few seconds and exits 0 with nothing printed."""
    assert cli.HOOK_SYNC_WAIT_MS <= 30_000 / 3
    monkeypatch.setattr(cli, "HOOK_SYNC_WAIT_MS", 200)
    root = tmp_path / "projects"
    transcript = root / "t.jsonl"
    _write(transcript, "t", ["a turn written before compaction ran"])
    db = tmp_path / "s.db"
    with Store(db):
        pass
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(transcript)})))
    locker = sqlite3.connect(db, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        started = time.perf_counter()
        code, out, err = _run(
            capsys, "--db", str(db), "sync", "--harness", "generic", "--from-hook"
        )
        took = time.perf_counter() - started
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert code == 0 and out == "" and err == "" and took < 2.0

    code, out, _ = _run(
        capsys, "--db", str(db), "sync", str(transcript), "--harness", "generic", "--json"
    )
    assert code == 0 and json.loads(out)["added"] == 1


@pytest.mark.parametrize("text", ["a", "s", "true", "null"])  # none inside "[removed]" itself
def test_json_stays_json_whatever_the_text(tmp_path, capsys, text):
    """The text is withheld in the JSON's values, not in its keys or its true, false and null."""
    db = _store(tmp_path)
    code, out, _ = _run(capsys, "--db", str(db), "prune", "--turns-containing", text, "--json")
    body = json.loads(out)
    assert code == 0 and body["applied"] is False and "turns_removed" in body
    assert all(
        text not in str(v) for r in body["retract"] for v in r.values() if isinstance(v, str)
    )


@pytest.mark.parametrize(
    "text",
    ["HUNTER2SECRET!", "my  pass", "Tok\tEN42x", "(sk-live_X9)"],
)
def test_a_printed_key_never_holds_the_text_that_normalizing_would_change(text):
    """``make_key`` lowercases, strips edge punctuation and collapses whitespace. A key made from
    the raw subject and only then withheld prints ``hunter2secret`` for ``HUNTER2SECRET!`` -- no
    withheld form matches it any more. The key is made from the already-withheld fields, so the
    text is gone before normalizing can change it."""
    import dataclasses

    from memware.ledger import Retraction, normalize

    row = {
        "id": 1,
        "subject": text,
        "relation": "r",
        "value": "v",
        "key": "",
        "source": "memware:session/s/turn/1",
    }
    kw: dict = {f.name: [] for f in dataclasses.fields(Retraction)}
    kw["retract"] = [row]
    key = cli._withheld_plan(Retraction(**kw), text).retract[0]["key"]
    assert normalize(text) not in key
    assert key == "[removed]|r"
