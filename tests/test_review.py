import http.server
import json
import stat
import sys
import threading

import pytest

from memware.cli import main
from memware.ledger import assert_belief, current
from memware.review import (
    Decision,
    HttpReviewBackend,
    JsonlReviewBackend,
    apply_decision,
    open_reviews,
    sync_reviews,
)
from memware.store import Store


def test_jsonl_backend_round_trip(store, tmp_path):
    assert_belief(store, "svc", "owner", "team-a", reliability=0.9)
    r = assert_belief(store, "svc", "owner", "team-b", reliability=0.2)
    assert len(open_reviews(store)) == 1
    outbox, inbox = tmp_path / "out.jsonl", tmp_path / "in.jsonl"
    be = JsonlReviewBackend(outbox, inbox)
    assert sync_reviews(store, be) == {"published": 1, "applied": 0}
    item = json.loads(outbox.read_text().splitlines()[0])
    assert item["candidate_value"] == "team-b" and item["incumbent_value"] == "team-a"
    inbox.write_text(json.dumps({"review_id": r.review_id, "decision": "approve"}) + "\n")
    assert sync_reviews(store, be) == {"published": 0, "applied": 1}
    assert [c["value"] for c in current(store, "svc")] == ["team-b"]
    assert inbox.read_text() == ""


def _contest(store, subject="svc"):
    """A committed belief and a low-reliability challenger parked for review; returns its id."""
    assert_belief(store, subject, "owner", "team-a", reliability=0.9)
    return assert_belief(store, subject, "owner", "team-b", reliability=0.2).review_id


def test_a_decision_the_ledger_cannot_apply_is_skipped_not_fatal(store, tmp_path):
    """The inbox may name a review someone already settled, or one that never existed. That
    line is dropped; the others in the same batch still apply and the inbox is emptied."""
    rid = _contest(store)
    inbox = tmp_path / "in.jsonl"
    inbox.write_text(
        json.dumps({"review_id": 9999, "decision": "approve"})
        + "\n"
        + json.dumps({"review_id": rid, "decision": "reject", "note": "not the owner"})
        + "\n"
    )
    be = JsonlReviewBackend(tmp_path / "out.jsonl", inbox)
    assert sync_reviews(store, be) == {"published": 0, "applied": 1}
    assert [c["value"] for c in current(store, "svc")] == ["team-a"]
    assert open_reviews(store) == []
    assert inbox.read_text() == ""


def test_apply_decision_rejects_and_refuses_an_unknown_verb(store):
    rid = _contest(store)
    assert apply_decision(store, Decision(rid, "reject")) == "reject"
    assert open_reviews(store) == []
    with pytest.raises(ValueError, match="unknown decision 'maybe'"):
        apply_decision(store, Decision(rid, "maybe"))


def test_jsonl_collect_skips_blank_lines_and_keeps_the_note(store, tmp_path):
    inbox = tmp_path / "in.jsonl"
    inbox.write_text(
        '\n  \n{"review_id": "7", "decision": "approve", "note": "checked"}\n\n'
        '{"review_id": 8, "decision": "reject"}\n'
    )
    be = JsonlReviewBackend(tmp_path / "out.jsonl", inbox)
    assert be.collect() == [Decision(7, "approve", "checked"), Decision(8, "reject", None)]
    assert be.collect() == []  # consumed
    assert JsonlReviewBackend(tmp_path / "o", tmp_path / "missing.jsonl").collect() == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_jsonl_outbox_is_private(store, tmp_path):
    """The outbox quotes belief values, so it is written owner-only in an owner-only folder."""
    _contest(store)
    outbox = tmp_path / "review" / "out.jsonl"
    JsonlReviewBackend(outbox, tmp_path / "in.jsonl").publish(open_reviews(store))
    assert stat.S_IMODE(outbox.stat().st_mode) == 0o600
    assert stat.S_IMODE(outbox.parent.stat().st_mode) == 0o700


class _Endpoint:
    """A loopback HTTP server standing in for a review service; records what it was sent."""

    def __init__(self, decisions):
        outer = self
        self.posts: list[tuple[str, dict, str | None]] = []
        self.decisions = decisions

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append((self.path, body, self.headers.get("Authorization")))
                self.send_response(204)  # an empty body is a valid acknowledgement
                self.end_headers()

            def do_GET(self):
                raw = json.dumps(outer.decisions).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def test_http_backend_posts_open_reviews_and_reads_decisions(store):
    rid = _contest(store)
    with _Endpoint({"decisions": [{"review_id": rid, "decision": "approve"}, "junk"]}) as srv:
        be = HttpReviewBackend(srv.url, token="s3cret")
        assert sync_reviews(store, be) == {"published": 0, "applied": 1}
        assert srv.posts == []  # nothing left open, so nothing published

        _contest(store, "other")
        assert sync_reviews(store, HttpReviewBackend(srv.url, token="s3cret")) == {
            "published": 1,
            "applied": 0,
        }
    ((path, body, auth),) = srv.posts
    assert path == "/api/reviews"  # the base's trailing slash is trimmed
    assert auth == "Bearer s3cret"
    assert body["items"][0]["subject"] == "other"
    assert [c["value"] for c in current(store, "svc")] == ["team-b"]


def test_http_backend_without_a_token_sends_no_authorization(store):
    _contest(store)
    with _Endpoint({"decisions": "not a list"}) as srv:
        be = HttpReviewBackend(srv.url)
        assert be.collect() == []  # a malformed payload is no decisions, not an error
        be.publish(open_reviews(store))
    assert srv.posts[0][2] is None


def test_cli_review_list_approve_and_reject(tmp_path, capsys):
    db = str(tmp_path / "r.db")
    with Store(db) as s:
        first = _contest(s, "one")
        second = _contest(s, "two")
    assert main(["--db", db, "review", "list"]) == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["subject"] for r in listed] == ["one", "two"]

    assert main(["--db", db, "--json", "review", "approve", str(first)]) == 0
    assert json.loads(capsys.readouterr().out)["review_id"] == first
    assert main(["--db", db, "review", "reject", str(second)]) == 0
    capsys.readouterr()
    with Store(db) as s:
        assert [c["value"] for c in current(s, "one")] == ["team-b"]
        assert [c["value"] for c in current(s, "two")] == ["team-a"]

    # settled reviews are no longer open: a clear message and exit 2, not a traceback
    assert main(["--db", db, "review", "approve", str(first)]) == 2
    assert f"no open review {first}" in capsys.readouterr().err


def test_cli_review_sync_through_files_and_through_a_url(tmp_path, capsys):
    db = str(tmp_path / "r.db")
    with Store(db) as s:
        rid = _contest(s)
    out, inb = tmp_path / "out.jsonl", tmp_path / "in.jsonl"
    files = ["--db", db, "--json", "review", "sync", "--outbox", str(out), "--inbox", str(inb)]
    assert main(files) == 0
    assert json.loads(capsys.readouterr().out) == {"published": 1, "applied": 0}
    assert json.loads(out.read_text())["review_id"] == rid

    inb.write_text(json.dumps({"review_id": rid, "decision": "approve"}) + "\n")
    assert main(files) == 0
    assert json.loads(capsys.readouterr().out) == {"published": 0, "applied": 1}

    with Store(db) as s:
        _contest(s, "other")
    with _Endpoint({"decisions": []}) as srv:
        assert main(["--db", db, "--json", "review", "sync", "--url", srv.url, "--token", "t"]) == 0
    assert json.loads(capsys.readouterr().out) == {"published": 1, "applied": 0}
    assert srv.posts[0][2] == "Bearer t"
