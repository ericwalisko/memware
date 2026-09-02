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
