from memware.ingest import sync_file, sync_tree
from tests.conftest import write_claude_jsonl


def test_claude_parser_keeps_prose_skips_tools_and_injected_blocks(store, tmp_path):
    p = tmp_path / "s1.jsonl"
    write_claude_jsonl(
        p,
        "sess-1",
        [
            (
                "user",
                "2026-08-10T10:00:00Z",
                "<system-reminder>injected by the harness</system-reminder>",
            ),
            ("user", "2026-08-10T10:00:01Z", "please switch the api port to 8443 and tell me why"),
            (
                "assistant",
                "2026-08-10T10:00:05Z",
                "Switched the api port to 8443 because 8080 collided.",
            ),
            ("assistant", "2026-08-10T10:00:06Z", "ok"),
        ],
    )
    assert sync_file(store, p, harness="claude-code") == 2
    rows = store.conn.execute("SELECT role, text FROM turn ORDER BY seq").fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert "8443" in rows[1]["text"]


def test_sync_is_idempotent_and_appends_only_new_lines(store, tmp_path):
    p = tmp_path / "s2.jsonl"
    write_claude_jsonl(
        p, "sess-2", [("user", "2026-08-11T00:00:00Z", "first question about the build")]
    )
    assert sync_file(store, p, harness="claude-code") == 1
    assert sync_file(store, p, harness="claude-code") == 0
    with p.open("a", encoding="utf-8") as fh:
        fh.write(
            '{"type":"assistant","sessionId":"sess-2","timestamp":"2026-08-11T00:00:05Z",'
            '"message":{"role":"assistant","content":[{"type":"text","text":"the build uses make and caches under .build"}]}}\n'
        )
    assert sync_file(store, p, harness="claude-code") == 1
    assert store.stats()["turns"] == 2


def test_truncated_file_is_reindexed_from_scratch(store, tmp_path):
    p = tmp_path / "s3.jsonl"
    write_claude_jsonl(
        p, "sess-3", [("user", "2026-08-12T00:00:00Z", "a long enough first line of text here")]
    )
    sync_file(store, p, harness="claude-code")
    write_claude_jsonl(p, "sess-3", [])  # rewritten shorter than the cursor
    sync_file(store, p, harness="claude-code")
    n = store.conn.execute(
        "SELECT count(*) FROM turn WHERE source=?", (str(p.resolve()),)
    ).fetchone()[0]
    assert n == 0


def test_generic_parser_and_tree_sync(store, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.jsonl").write_text(
        '{"role":"user","content":"where do we keep the release checklist","timestamp":"2026-08-01T00:00:00Z"}\n'
        '{"role":"assistant","content":[{"type":"text","text":"the release checklist lives in docs/release.md"}]}\n'
        '{"role":"system","content":"ignored"}\n',
        encoding="utf-8",
    )
    assert sum(sync_tree(store, tmp_path, harness="generic").values()) == 2


def test_skip_if_contains_and_prune(store, tmp_path, monkeypatch):
    from memware.ingest import prune_sources, sync_tree

    good, evalrun = tmp_path / "good.jsonl", tmp_path / "evalrun.jsonl"
    write_claude_jsonl(
        good,
        "g",
        [("assistant", "2026-08-10T00:00:00Z", "the real work happened here in the good session")],
    )
    write_claude_jsonl(
        evalrun,
        "e",
        [
            (
                "user",
                "2026-08-11T00:00:00Z",
                "[memware-eval] Answer briefly: which port does the api use",
            ),
            ("assistant", "2026-08-11T00:00:01Z", "the api uses port 8443 according to my notes"),
        ],
    )
    assert sum(sync_tree(store, tmp_path, harness="claude-code").values()) == 3
    # retroactive: un-index everything carrying the marker
    rep = prune_sources(store, containing="[memware-eval]")
    assert sum(rep.values()) == 2 and store.stats()["turns"] == 1
    # preventive: a resync with the marker filter never brings it back
    assert (
        sum(
            sync_tree(
                store, tmp_path, harness="claude-code", skip_if_contains="[memware-eval]"
            ).values()
        )
        == 0
    )
    assert sum(sync_tree(store, tmp_path, harness="claude-code", exclude=["*good*"]).values()) == 2


def test_hook_sync_is_a_noop_under_no_capture(tmp_path, monkeypatch, capsys):
    import io
    import json
    import sys

    from memware.cli import main

    p = tmp_path / "s.jsonl"
    write_claude_jsonl(
        p,
        "s",
        [("assistant", "2026-08-20T00:00:00Z", "this eval run must not be indexed anywhere")],
    )
    monkeypatch.setenv("MEMWARE_NO_CAPTURE", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"transcript_path": str(p)})))
    db = str(tmp_path / "n.db")
    assert main(["--db", db, "sync", "--from-hook"]) == 0
    from memware.store import Store

    with Store(db) as s:
        assert s.stats()["turns"] == 0


def test_persistent_ignore_markers(store, tmp_path, monkeypatch):
    from memware.ingest import sync_file

    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "ignore-markers.txt").write_text(
        "# eval runs\n[memware-eval]\nAnswer briefly using only\n"
    )
    good = tmp_path / "good.jsonl"
    write_claude_jsonl(
        good,
        "g",
        [("assistant", "2026-08-01T00:00:00Z", "the real work of this session lives here")],
    )
    old_eval = tmp_path / "old.jsonl"
    write_claude_jsonl(
        old_eval,
        "o",
        [("user", "2026-08-01T00:00:00Z", "Answer briefly using only what you know: which port")],
    )
    assert sync_file(store, good, harness="claude-code") == 1
    assert sync_file(store, old_eval, harness="claude-code") == 0  # matched a persistent marker
    # env var adds markers too
    monkeypatch.setenv("MEMWARE_IGNORE_MARKERS", "real work of this session")
    assert sync_file(store, good, harness="claude-code") == 0  # now excluded; prior turns pruned
    assert store.stats()["turns"] == 0
