"""What ``memware prune`` selects, and what it says when it selects nothing.

Pins #32 (``--turns-containing`` matched only a prefix) and #33 (``--containing`` read only the
first 200 KB of a transcript). Both answered a confident 0, which reads the same as a clean
store to someone removing a leaked value. Every store here is synthetic, under the test's own
tmp dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memware.cli import main
from memware.ingest import _file_holds, file_contains, prune, prune_turns, sync_tree
from memware.store import Store

MID, REPLY = (
    "the deploy target is SECRET123 and we should keep it",
    "understood, the target SECRET123 is recorded here",
)


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _corpus(
    root: Path, name: str, contents: list[str], roles: tuple[str, ...] = ("user", "assistant")
) -> Path:
    """A generic-harness transcript, one message per line, taking ``roles`` in turn."""
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_text(
        "".join(
            json.dumps({"role": roles[i % len(roles)], "content": text}) + "\n"
            for i, text in enumerate(contents)
        ),
        encoding="utf-8",
    )
    return path


def _db(
    tmp_path: Path,
    contents: list[str],
    name: str = "small.jsonl",
    roles: tuple[str, ...] = ("user", "assistant"),
) -> str:
    root = tmp_path / "corpus"
    _corpus(root, name, contents, roles)
    db = str(tmp_path / "scratch.db")
    with Store(db) as s:
        sync_tree(s, root, harness="generic")
    return db


def _texts(db: str) -> list[str]:
    with Store(db) as s:
        return [r["text"] for r in s.conn.execute("SELECT text FROM turn ORDER BY id")]


def _json(capsys, db: str, *argv: str) -> dict[str, object]:
    code, out, _ = _run(capsys, "--db", db, "prune", *argv, "--json")
    assert code == 0
    parsed: dict[str, object] = json.loads(out)
    return parsed


def test_turns_containing_removes_a_value_held_mid_turn(tmp_path, capsys):
    """#32: the issue's reproduction. Neither turn starts with the value."""
    db = _db(tmp_path, [MID, REPLY])
    assert _json(capsys, db, "--turns-containing", "SECRET123")["turns_removed"] == 2
    assert _json(capsys, db, "--turns-starting-with", "SECRET123")["turns_removed"] == 0
    assert _texts(db) == [MID, REPLY]  # both dry runs wrote nothing

    r = _json(capsys, db, "--turns-containing", "SECRET123", "--apply")
    assert r["turns_removed"] == 2
    assert not any("SECRET123" in t for t in _texts(db))


def test_turns_starting_with_keeps_the_prefix_behaviour(tmp_path, capsys):
    db = _db(tmp_path, [MID, REPLY])
    r = _json(capsys, db, "--turns-starting-with", "the deploy target is SECRET123", "--apply")
    assert r["turns_removed"] == 1
    assert _texts(db) == [REPLY]


def test_prune_turns_takes_a_prefix_or_a_substring(store, tmp_path):
    root = tmp_path / "corpus"
    _corpus(root, "s.jsonl", [MID, REPLY, "the deploy target moves to SECRET456 on friday"])
    sync_tree(store, root, harness="generic")
    assert prune_turns(store, starting_with="the deploy target") == 2
    assert prune_turns(store, containing="SECRET123") == 1
    assert store.stats()["turns"] == 0


def test_turn_selectors_match_literally_and_case_sensitively(tmp_path):
    db = _db(
        tmp_path,
        [
            "export API_KEY=abc before the deploy runs",
            "the APIXKEY variable is unrelated to this",
            "rollout reached 100% of hosts this morning",
            "rollout reached 1000 of hosts this morning",
            "api_key in lower case is a different string",
        ],
    )
    with Store(db) as s:
        assert prune(s, turns_containing="API_KEY").turns == 1  # `_` is not a wildcard
        assert prune(s, turns_containing="100%").turns == 1  # nor is `%`
        assert prune(s, turns_starting_with="ROLLOUT").turns == 0  # case matters
        assert prune(s, turns_starting_with="rollout reached 10").turns == 2


def test_containing_reads_past_the_first_200_kb(tmp_path, capsys):
    """#33: the issue's reproduction. The marker sits past ``file_contains``'s head window."""
    filler = "x" * 900
    contents = [f"filler line {i} {filler}" for i in range(400)]
    contents.append("the deploy target is LATEMARK42 near the end")
    db = _db(tmp_path, contents, name="big.jsonl", roles=("user",))
    big = tmp_path / "corpus" / "big.jsonl"
    assert big.read_bytes().index(b"LATEMARK42") == 379_140

    r = _json(capsys, db, "--containing", "LATEMARK42")
    assert (r["sources_pruned"], r["turns_removed"]) == (1, 401)
    # sync's skip markers and the backup mirror still check only the head, as documented
    assert not file_contains(big, "LATEMARK42")
    assert file_contains(big, "filler line 5 ")


@pytest.mark.parametrize("chunk_bytes", [1, 2, 3, 5, 9, 10, 11, 64])
def test_a_marker_split_across_chunks_still_matches(tmp_path, chunk_bytes):
    marker = "SPLIT-MARK"
    for lead in range(len(marker) + 2):
        p = tmp_path / f"f{lead}.jsonl"
        p.write_bytes(b"a" * lead + marker.encode() + b"b" * 7)
        assert _file_holds(p, marker, chunk_bytes=chunk_bytes), (lead, chunk_bytes)
        assert not _file_holds(p, "SPLIT-MARKS", chunk_bytes=chunk_bytes)


def test_a_zero_match_says_what_was_searched_and_names_the_alternative(tmp_path, capsys):
    db = _db(tmp_path, [MID, REPLY])

    code, out, err = _run(capsys, "--db", db, "prune", "--turns-starting-with", "SECRET123")
    assert code == 0 and "turns to remove : 0" in out
    assert (
        "no turn starts with 'SECRET123' (2 searched, matched literally and case-sensitively); "
        "2 turns contain it past the start: try --turns-containing"
    ) in err

    _, _, err = _run(capsys, "--db", db, "prune", "--turns-containing", "secret123")
    assert "no turn contains 'secret123' (2 searched" in err
    assert "2 turns contain it in another case" in err

    _, _, err = _run(capsys, "--db", db, "prune", "--turns-starting-with", "NOWHERE99")
    assert "no turn starts with or contains 'NOWHERE99' (2 searched" in err

    _, _, err = _run(capsys, "--db", db, "prune", "--containing", "NOWHERE99")
    assert (
        "no indexed source contains 'NOWHERE99': 1 transcript file read whole, "
        "matched literally and case-sensitively"
    ) in err

    _, _, err = _run(capsys, "--db", db, "prune", "--glob", "*nomatch*")
    assert "no path of the 1 indexed source matches '*nomatch*'" in err


def test_a_gone_transcript_is_named_and_its_turns_pointed_at(tmp_path, capsys):
    db = _db(tmp_path, [MID, REPLY])
    (tmp_path / "corpus" / "small.jsonl").unlink()
    code, out, err = _run(capsys, "--db", db, "prune", "--containing", "SECRET123")
    assert code == 0 and "sources to un-index : 0" in out
    assert "no indexed source contains 'SECRET123': 0 transcript files read whole" in err
    assert "1 indexed source not read: the transcript file is gone" in err
    assert "2 indexed turns contain it: try --turns-containing" in err

    with Store(db) as s:
        assert prune(s, containing="SECRET123").missing == (
            str((tmp_path / "corpus" / "small.jsonl").resolve()),
        )


def test_a_prefix_names_the_turns_it_leaves_holding_the_text(tmp_path, capsys):
    db = _db(tmp_path, [MID, REPLY, "SECRET123 was rotated this morning, confirm it"])
    code, out, err = _run(capsys, "--db", db, "prune", "--turns-starting-with", "SECRET123")
    assert "turns to remove : 1" in out
    assert (
        "2 more turns contain 'SECRET123' past the start and are kept: "
        "--turns-containing selects them too"
    ) in err
    # a selector that took everything it names has nothing to add
    _, _, err = _run(capsys, "--db", db, "prune", "--turns-containing", "SECRET123")
    assert err == "dry run: nothing written; add --apply to write it\n"


def test_turn_selectors_take_no_other_selector(tmp_path, capsys):
    db = _db(tmp_path, [MID, REPLY])
    for extra in (["--glob", "*"], ["--containing", "SECRET123"], ["--turns-starting-with", "x"]):
        code, _, err = _run(
            capsys, "--db", db, "prune", "--turns-containing", "SECRET123", *extra, "--apply"
        )
        assert code == 2 and "take no other selector" in err
    code, _, err = _run(capsys, "--db", db, "prune", "--apply")
    assert code == 2 and "--turns-starting-with" in err
    assert _texts(db) == [MID, REPLY]

    with Store(db) as s:
        with pytest.raises(ValueError, match="no other selector"):
            prune(s, glob="*", turns_starting_with="the")
        with pytest.raises(ValueError, match="every turn"):
            prune(s, turns_containing="")
        with pytest.raises(ValueError, match="exactly one"):
            prune_turns(s)
    assert _texts(db) == [MID, REPLY]
