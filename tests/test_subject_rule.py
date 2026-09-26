"""The subject rule for unsolicited injection (issue #47): a belief whose subject shares only one
word with the prompt is injected only if that word is rare in the user's own conversations.

Stores here are built turn by turn with short texts, so each turn is one passage and the shares
are exact: "file" is in 40% of passages, "config" in 20%, "kanban" in 100%, "widget" in 2%."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memware import index
from memware.cli import main
from memware.index import RARE_SHARE, RARITY_MIN_PASSAGES, search_beliefs, subject_passes
from memware.ledger import assert_belief
from memware.passage import index_turn
from memware.store import Store

TO_FILE = "any feedback to file about widget service?"
TO_SUBMIT = "any feedback to submit about widget service?"
FILLER = "deploy cache retry queue parser token branch lint".split()


def _add_turns(store: Store, start: int, n: int) -> None:
    for i in range(start, start + n):
        words = [FILLER[i % len(FILLER)], FILLER[(i + 3) % len(FILLER)], "kanban", "memware"]
        if i % 5 < 2:
            words.append("file")
        if i % 5 == 2:
            words.append("config")
        if i % 50 == 0:
            words.append("widget")
        cur = store.conn.execute(
            "INSERT INTO turn(session, seq, ts, role, text, source, harness) "
            "VALUES (?, ?, '2026-09-01T00:00:00Z', 'user', ?, ?, 'claude-code')",
            (f"s{i // 20}", i, " ".join(words), f"/t/s{i // 20}.jsonl"),
        )
        index_turn(store.conn, int(cur.lastrowid or 0), " ".join(words))


def _store(path: Path, passages: int) -> Store:
    """The four beliefs of issue #47 over ``passages`` one-passage turns."""
    s = Store(path)
    _add_turns(s, 0, passages)
    for subject, relation, value in (
        ("config file location", "is", "etc/app/conf.d"),
        ("export job file retention", "is", "7 days"),
        ("widget service schema migration", "added", "two tables, no rows lost"),
        ("widget service prune", "use case", "a value pasted mid-conversation"),
    ):
        assert_belief(s, subject, relation, value, valid_from="2026-09-01T00:00:00Z")
    assert s.stats()["passages"] == passages
    return s


def _subjects(store: Store, prompt: str) -> set[str]:
    return {
        h.subject or ""
        for h in search_beliefs(store, prompt, k=10, require_subject=True, record_use=False)
    }


WIDGETS = {"widget service schema migration", "widget service prune"}
FILES = {"config file location", "export job file retention"}


@pytest.fixture()
def big(tmp_path: Path):
    s = _store(tmp_path / "big.db", 1_200)
    yield s
    s.close()


def test_the_shares_are_what_the_corpus_says(big):
    vocab = index._vocab(big)
    assert vocab.passages == 1_200
    assert vocab.share("file") == pytest.approx(0.4)
    assert vocab.share("config") == pytest.approx(0.2)
    assert vocab.share("widget") == pytest.approx(0.02)
    assert vocab.share("kanban") == 1.0


def test_issue_47_to_file_injects_only_the_widget_beliefs_and_to_submit_is_unchanged(big):
    assert _subjects(big, TO_FILE) == WIDGETS
    assert _subjects(big, TO_SUBMIT) == WIDGETS


def test_issue_47_through_the_prompt_hook(big, capsys):
    big.close()
    for prompt in (TO_FILE, TO_SUBMIT):
        assert main(["--db", str(big.path), "context", prompt, "-k", "6"]) == 0
        out = capsys.readouterr().out
        assert all(w in out for w in WIDGETS), prompt
        assert not any(f in out for f in FILES), prompt


def test_one_rare_shared_word_passes(big):
    assert _subjects(big, "is the widget ready?") == WIDGETS


def test_one_common_shared_word_fails(big):
    assert _subjects(big, "please file it") == set()
    assert not subject_passes(big, "please file it", "config file location")


def test_two_shared_words_pass_though_both_are_common(big):
    # "config" (20%) and "file" (40%) are each common; together they name the subject
    assert _subjects(big, "where does the config file live?") == {"config file location"}
    assert subject_passes(big, "where does the config file live?", "config file location")


def test_a_word_every_passage_holds_does_not_carry_a_belief_alone(big):
    assert_belief(big, "kanban release", "cadence", "weekly", valid_from="2026-09-01T00:00:00Z")
    assert _subjects(big, "how is kanban doing") == set()
    assert _subjects(big, "when is the next kanban release") == {"kanban release"}


def test_a_word_is_looked_up_as_written_so_a_stemmed_word_reads_as_rare(big):
    """The passage index keeps porter stems: every passage holds "memware", and the index holds
    "memwar". Looked up as written, "memware" is absent and counts as rare. That is the rule as it
    was measured (docs/relevance-calibration.md); a stem-aware lookup would be a new rule."""
    assert index._vocab(big).share("memware") == 0.0
    assert_belief(big, "memware release", "cadence", "weekly", valid_from="2026-09-01T00:00:00Z")
    assert _subjects(big, "how is memware doing") == {"memware release"}


@pytest.mark.parametrize(
    ("subject", "prompt"),
    [
        ("memware-0.7.0 wheel", "did memware-0.7.0 ship?"),  # the index holds memware, 0, 7
        ("etc/app/conf.d", "what lives in etc/app/conf.d"),  # and etc, app, conf, d
        ("t_b6b2f934", "status of t_b6b2f934"),
    ],
)
def test_a_term_the_index_does_not_hold_as_written_counts_as_rare(big, subject, prompt):
    assert_belief(big, subject, "is", "x", valid_from="2026-09-01T00:00:00Z")
    assert subject in _subjects(big, prompt)


def test_no_shared_word_fails_whatever_the_store(big, tmp_path):
    assert not subject_passes(big, "a relation word only", "config file location")
    with _store(tmp_path / "small.db", 10) as small:
        assert not subject_passes(small, "a relation word only", "config file location")


@pytest.mark.parametrize(
    ("passages", "lone_common_word_passes"),
    [(RARITY_MIN_PASSAGES - 1, True), (RARITY_MIN_PASSAGES, False)],
)
def test_the_small_store_floor(tmp_path, passages, lone_common_word_passes):
    """Below the floor one shared word passes, as before the rule; from the floor on it must be
    rare. The shares are the same on both sides: only the number of passages changes."""
    with _store(tmp_path / "s.db", passages) as s:
        assert index._vocab(s).share("file") > RARE_SHARE
        expected = FILES | WIDGETS if lone_common_word_passes else WIDGETS
        assert _subjects(s, TO_FILE) == expected
        assert _subjects(s, TO_SUBMIT) == WIDGETS


def test_a_store_with_no_conversations_keeps_todays_behaviour(tmp_path):
    """The issue's own repro indexes no conversations, so there is no share to read."""
    with _store(tmp_path / "s.db", 0) as s:
        assert _subjects(s, TO_FILE) == FILES | WIDGETS


def test_the_counts_follow_the_store_as_it_grows(tmp_path):
    """Cached per store, and read again once the store changes: crossing the floor on a store
    that stays open applies the rule from the next search on."""
    with _store(tmp_path / "s.db", RARITY_MIN_PASSAGES - 1) as s:
        assert _subjects(s, TO_FILE) == FILES | WIDGETS
        _add_turns(s, RARITY_MIN_PASSAGES - 1, 1)
        assert _subjects(s, TO_FILE) == WIDGETS


def test_another_connection_writing_invalidates_the_cache(tmp_path):
    with _store(tmp_path / "s.db", RARITY_MIN_PASSAGES - 1) as s:
        assert _subjects(s, TO_FILE) == FILES | WIDGETS
        with Store(s.path) as writer:
            _add_turns(writer, RARITY_MIN_PASSAGES - 1, 1)
        assert _subjects(s, TO_FILE) == WIDGETS


def test_each_term_is_read_once_per_store(big):
    reads: list[str] = []
    big.conn.set_trace_callback(reads.append)
    for _ in range(3):
        _subjects(big, TO_FILE)
        subject_passes(big, TO_FILE, "config file location")
    big.conn.set_trace_callback(None)
    assert sum("memware_passage_vocab WHERE" in r for r in reads) == 1
    assert sum("count(*) FROM passage" in r for r in reads) == 1


def test_the_rule_runs_on_a_read_only_connection_and_writes_nothing(big):
    big.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    path = big.path
    big.close()
    before = path.read_bytes()
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("DELETE FROM belief")

        class ReadOnly:  # what a caller outside memware passes: an object holding the connection
            conn = ro

        for store in (ro, ReadOnly()):
            assert subject_passes(store, TO_FILE, "widget service prune")
            assert not subject_passes(store, TO_FILE, "config file location")
            assert subject_passes(store, TO_FILE, "config file location") is False
    finally:
        ro.close()
    assert path.read_bytes() == before


def test_prefetch_in_hermes_applies_the_rule(tmp_path):
    from tests.test_hermes_plugin import load

    _store(tmp_path / "m.db", 1_200).close()
    _, provider = load(tmp_path)
    block = provider.prefetch(TO_FILE)
    assert all(w in block for w in WIDGETS)
    assert not any(f in block for f in FILES)


def test_eval_retrieval_applies_the_rule(big):
    from memware.eval import retrieve

    beliefs, _ = retrieve(big, TO_FILE, k=8)
    assert all(w in beliefs for w in WIDGETS)
    assert not any(f in beliefs for f in FILES)


def test_nuke_removes_the_labels_directory(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("MEMWARE_HOME", str(home))
    db = tmp_path / "m.db"
    Store(db).close()
    labels = home / "labels"
    (labels / "batch").mkdir(parents=True)
    (labels / "pairs.jsonl").write_text('{"prompt": "a prompt someone typed"}\n')
    (labels / "batch" / "labels-1.jsonl").write_text('{"relevant": true}\n')
    assert main(["--db", str(db), "nuke", "--confirm", "DELETE ALL MEMWARE DATA", "--json"]) == 0
    out = capsys.readouterr().out
    assert f"labels:     {labels}/" in out
    assert not labels.exists() and not db.exists()
    assert json.loads(out[out.index("{") :])["deleted_files"] == 3  # the store and two labels


def test_nuke_removes_a_labels_link_without_following_it(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MEMWARE_HOME", str(home))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("not memware's")
    (home / "labels").symlink_to(elsewhere, target_is_directory=True)
    db = tmp_path / "m.db"
    Store(db).close()
    assert main(["--db", str(db), "nuke", "--confirm", "DELETE ALL MEMWARE DATA"]) == 0
    capsys.readouterr()
    assert not (home / "labels").is_symlink()
    assert (elsewhere / "keep.txt").read_text() == "not memware's"


def test_nuke_without_labels_says_nothing_about_them(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    db = tmp_path / "m.db"
    Store(db).close()
    assert main(["--db", str(db), "nuke", "--confirm", "DELETE ALL MEMWARE DATA"]) == 0
    assert "labels:" not in capsys.readouterr().out
