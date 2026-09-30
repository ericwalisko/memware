"""An MCP tool fires only when the model elects to call it, and the description is all it has to
decide with. recall competes with grep, which is in context, faster, and usually right, so its
description leads with when to call it rather than how. Descriptions are read on every turn, so
these tests also hold recall to a word budget: they erode one helpful sentence at a time."""

import asyncio
import json

import pytest

from memware.mcp_server import build

pytest.importorskip("mcp")


def _descriptions() -> dict[str, str]:
    return {t.name: t.description or "" for t in asyncio.run(build().list_tools())}


def test_recall_leads_with_when_to_call_it():
    d = _descriptions()["recall"]
    assert d.startswith("Call this when"), d
    assert "Not for:" in d, d


def test_recall_description_stays_under_budget():
    words = len(_descriptions()["recall"].split())
    assert words < 130, words


def test_beliefs_and_read_session_say_when_to_call_them():
    d = _descriptions()
    for name in ("beliefs", "read_session"):
        assert d[name].startswith("Call this"), d[name]


def _call(app, name, **args):
    """Call a tool through the server as a client would; return the decoded JSON payloads, one
    per content block (a list result is one block per item)."""
    res = asyncio.run(app.call_tool(name, args))
    content = res[0] if isinstance(res, tuple) else getattr(res, "content", res)
    return [json.loads(block.text) for block in content]


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """The server bound to a fresh store, which it reads from MEMWARE_DB when it is built."""
    monkeypatch.setenv("MEMWARE_DB", str(tmp_path / "mcp.db"))
    return build()


def test_the_server_offers_exactly_these_five_tools(app):
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert names == {"recall", "read_session", "beliefs", "remember", "pending_reviews"}


def test_remember_then_beliefs_then_recall_through_the_server(app):
    (made,) = _call(app, "remember", subject="api", relation="listens on", value="8443")
    assert made["outcome"] == "created" and made["review_id"] is None

    (belief,) = _call(app, "beliefs", subject="api")
    assert belief["value"] == "8443" and belief["status"] == "committed"
    assert _call(app, "beliefs", subject="nothing-here") == []
    assert [b["value"] for b in _call(app, "beliefs")] == ["8443"]  # no subject: all of them

    hits = _call(
        app, "recall", queries=["what port does api listen on", "api 8443"], what="beliefs"
    )
    assert hits and hits[0]["kind"] == "belief" and "8443" in hits[0]["text"]
    assert _call(app, "recall", queries=["api"], what="turns") == []  # nothing indexed yet


def test_a_contested_remember_lands_in_pending_reviews(app):
    _call(app, "remember", subject="svc", relation="owner", value="team-a", reliability=0.9)
    (contested,) = _call(
        app, "remember", subject="svc", relation="owner", value="team-b", reliability=0.2
    )
    assert contested["review_id"] is not None and contested["outcome"] != "created"

    (pending,) = _call(app, "pending_reviews")
    assert pending["review_id"] == contested["review_id"]
    assert (pending["candidate_value"], pending["incumbent_value"]) == ("team-b", "team-a")
    assert [b["value"] for b in _call(app, "beliefs", subject="svc")] == ["team-a"]


def test_read_session_returns_a_window_around_a_turn_hit(app, tmp_path):
    from memware.ingest import sync_file
    from memware.store import Store
    from tests.conftest import write_claude_jsonl

    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"]
    texts = [f"the {w} step of the api migration" for w in words]
    turns = [
        ("user" if i % 2 == 0 else "assistant", f"2026-08-01T00:00:{i:02d}Z", text)
        for i, text in enumerate(texts)
    ]
    transcript = tmp_path / "s.jsonl"
    write_claude_jsonl(transcript, "sess-1", turns)
    with Store(tmp_path / "mcp.db") as s:
        sync_file(s, transcript, harness="claude-code")

    assert [t["text"] for t in _call(app, "read_session", session="sess-1")] == texts
    (hit,) = _call(app, "recall", queries=["echo"], what="turns")
    assert hit["text"] == texts[4]
    near = _call(app, "read_session", session="sess-1", around=hit["id"], window=1)
    assert [t["text"] for t in near] == texts[3:6]
    assert _call(app, "read_session", session="no-such-session") == []


def test_main_serves_the_built_app(monkeypatch):
    import memware.mcp_server as server

    served = []

    class App:
        def run(self) -> None:
            served.append(True)

    monkeypatch.setattr(server, "build", App)
    server.main()
    assert served == [True]
