"""`memware injections`: what the prompt hook injected, read back from Claude Code transcripts,
paired with the prompt a person typed and matched to the belief it came from."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memware.cli import main
from memware.digest import belief_line
from memware.injections import block_lines, scan
from memware.ledger import Policy, assert_belief, retract
from memware.relevance import prompt_id, sent_prompt
from memware.store import Store
from tests.conftest import write_hook_transcript

TS = "2026-10-01T10:00:00.000Z"


def line(s: Store, belief_id: int) -> str:
    r = s.conn.execute("SELECT * FROM belief WHERE id=?", (belief_id,)).fetchone()
    return belief_line(r["subject"], r["relation"], r["value"], r["valid_from"])


@pytest.fixture()
def ledger(tmp_path: Path):
    s = Store(tmp_path / "m.db")
    ids = {
        "port": assert_belief(
            s, "api", "listens on port", "8080", valid_from="2026-09-01T00:00:00Z"
        ).belief_id,
        "region": assert_belief(
            s, "cluster", "region", "eu-west", valid_from="2026-09-02T00:00:00Z"
        ).belief_id,
        "retracted": assert_belief(
            s, "queue", "drain order", "fifo", valid_from="2026-09-03T00:00:00Z"
        ).belief_id,
    }
    old = {k: line(s, v) for k, v in ids.items()}
    # Superseded after the injection, and retracted after it: both still match what was injected.
    ids["port_new"] = assert_belief(
        s,
        "api",
        "listens on port",
        "8443",
        valid_from="2026-10-05T00:00:00Z",
        reliability=0.9,
        policy=Policy.AUTO,
    ).belief_id
    retract(s, beliefs={ids["retracted"]: "test"}, reason="test", apply=True)
    yield s, ids, old
    s.close()


def test_pairs_each_injected_line_with_the_typed_prompt_and_its_belief(ledger, tmp_path):
    s, ids, old = ledger
    t = tmp_path / "projects" / "p" / "s1.jsonl"
    write_hook_transcript(
        t,
        "s1",
        [
            (TS, "where does the api listen", [old["port"], old["region"]]),
            ("2026-10-02T10:00:00.000Z", "how is the queue drained", [old["retracted"]]),
            ("2026-10-03T10:00:00.000Z", "a prompt the hook added nothing to", []),
        ],
    )
    found, counts = scan(s.conn, tmp_path / "projects")
    assert [(i.prompt, i.rank, i.belief_id) for i in found] == [
        ("where does the api listen", 0, ids["port"]),  # superseded since, still the one injected
        ("where does the api listen", 1, ids["region"]),
        ("how is the queue drained", 0, ids["retracted"]),
    ]
    first = found[0]
    assert first.prompt_id == prompt_id(sent_prompt("where does the api listen"))
    assert first.pair_id == f"{first.prompt_id}:{ids['port']}"
    assert first.block == 2 and first.fact == "api listens on port: 8080"
    # Another plugin's context on the same event, and the tool hooks, are not memware's.
    assert counts.blocks == 2 and counts.lines == 3 and counts.unmatched == 0


def test_a_line_no_belief_renders_to_is_counted_not_paired(ledger, tmp_path):
    s, ids, old = ledger
    gone = "- deleted belief was: here (recorded 2026-09-09)"
    write_hook_transcript(tmp_path / "p" / "s.jsonl", "s", [(TS, "prompt", [gone, old["region"]])])
    found, counts = scan(s.conn, tmp_path / "p")
    assert [(i.rank, i.belief_id, i.pair_id is None) for i in found] == [
        (0, None, True),
        (1, ids["region"], False),
    ]
    assert counts.unmatched == 1


def test_an_undated_line_still_matches(ledger, tmp_path):
    s, ids, old = ledger
    undated = old["region"].rsplit(" (recorded ", 1)[0]
    write_hook_transcript(tmp_path / "p" / "s.jsonl", "s", [(TS, "prompt", [undated])])
    found, _ = scan(s.conn, tmp_path / "p")
    assert [i.belief_id for i in found] == [ids["region"]]


@pytest.mark.parametrize("entrypoint", ["sdk-cli", "sdk-py"])
def test_headless_sessions_are_skipped(ledger, tmp_path, entrypoint):
    s, _, old = ledger
    write_hook_transcript(
        tmp_path / "p" / "s.jsonl", "s", [(TS, "prompt", [old["region"]])], entrypoint=entrypoint
    )
    found, counts = scan(s.conn, tmp_path / "p")
    assert found == [] and counts.headless == 1


def test_a_sidechain_is_skipped(ledger, tmp_path):
    s, _, old = ledger
    write_hook_transcript(
        tmp_path / "p" / "s.jsonl", "s", [(TS, "prompt", [old["region"]])], sidechain=True
    )
    assert scan(s.conn, tmp_path / "p")[0] == []


def test_what_memware_keeps_out_is_not_read(ledger, tmp_path, monkeypatch):
    s, _, old = ledger
    root = tmp_path / "projects"
    write_hook_transcript(root / "keep" / "a.jsonl", "a", [(TS, "prompt", [old["region"]])])
    write_hook_transcript(root / "secret" / "b.jsonl", "b", [(TS, "prompt", [old["region"]])])
    write_hook_transcript(
        root / "keep" / "c.jsonl", "c", [(TS, "prompt ZZ-IGNORE-ME", [old["region"]])]
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MEMWARE_HOME", str(home))
    (home / "config.json").write_text(json.dumps({"capture": {"exclude": ["*/secret/*"]}}))
    (home / "ignore-markers.txt").write_text("ZZ-IGNORE-ME\n")
    found, counts = scan(s.conn, root)
    assert {i.session for i in found} == {"a"}
    assert counts.kept_out == 2  # the excluded path and the transcript holding a marker


def test_the_window_reads_only_recent_injections(ledger, tmp_path):
    s, _, old = ledger
    now = datetime.now(UTC)
    stamp = lambda d: (now - timedelta(days=d)).isoformat().replace("+00:00", "Z")  # noqa: E731
    write_hook_transcript(
        tmp_path / "p" / "s.jsonl",
        "s",
        [(stamp(20), "old", [old["region"]]), (stamp(1), "new", [old["region"]])],
    )
    found, _ = scan(s.conn, tmp_path / "p", since=now - timedelta(days=14))
    assert [i.prompt for i in found] == ["new"]


def test_block_lines_reads_only_memware_blocks():
    from memware.digest import CONTEXT_TITLE

    assert block_lines(["other\n- x"]) is None
    assert block_lines(f"{CONTEXT_TITLE}\n- a b: c\n- d e: f\n\ntrailing") == [
        "- a b: c",
        "- d e: f",
    ]
    assert block_lines(42) is None


def test_cli_prints_counts_by_default_and_rows_with_jsonl(ledger, tmp_path, capsys):
    s, ids, old = ledger
    s.close()
    write_hook_transcript(tmp_path / "p" / "s.jsonl", "s", [(TS, "zebra prompt", [old["region"]])])
    db = str(tmp_path / "m.db")
    base = ["--db", db, "injections", "--transcript-src", str(tmp_path / "p")]
    assert main(base) == 0
    out = capsys.readouterr().out
    assert "zebra" not in out
    summary = json.loads(out)
    assert summary["prompts"] == 1 and summary["pairs"] == 1 and summary["lines_unmatched"] == 0
    assert main([*base, "--jsonl"]) == 0
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [(r["prompt"], r["belief_id"], r["rank"], r["block"]) for r in rows] == [
        ("zebra prompt", ids["region"], 0, 1)
    ]


def test_cli_reads_the_configured_transcript_source(ledger, tmp_path, capsys, monkeypatch):
    s, _, old = ledger
    s.close()
    write_hook_transcript(tmp_path / "src" / "s.jsonl", "s", [(TS, "p", [old["region"]])])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MEMWARE_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"backup": {"transcript_src": str(tmp_path / "src")}})
    )
    assert main(["--db", str(tmp_path / "m.db"), "injections"]) == 0
    assert json.loads(capsys.readouterr().out)["pairs"] == 1
