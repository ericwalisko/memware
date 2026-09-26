"""The optional relevance filter (memware.relevance): off by default, and fails open.

The default must not change a byte for a prompt a person typed, or open a socket, so those tests
compare against output captured from origin/main (``tests/data/relevance_ledger.json``) under a
guard that records any attempt to resolve or connect. The one exception is a turn nobody typed
(``relevance.typed``), which now gets nothing in every mode, the default included. The rest run
against a local stand-in for TypeSafe's endpoint that
answers by subject, stalls, errors, or replies with the wrong shape, as each test asks.
"""

from __future__ import annotations

import http.server
import io
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from memware import relevance
from memware.cli import main
from memware.config import memware_home
from memware.ledger import assert_belief
from memware.store import Store

LEDGER = json.loads((Path(__file__).parent / "data" / "relevance_ledger.json").read_text())
PROMPT: str = LEDGER["prompt"]
RELEVANT: list[str] = LEDGER["relevant"]
HERMES = Path(__file__).resolve().parents[1] / "integrations" / "hermes" / "memware" / "__init__.py"
KEY = "sk-test-not-a-real-key"


class QuietServer(http.server.ThreadingHTTPServer):
    """A client that gave up on a slow reply is not worth a traceback: printed to stderr, it lands
    in whichever later test is capturing stderr at the time."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class FakeJev:
    """Answers each noul by the subject its fact starts with, as :attr:`p` says (default 0)."""

    def __init__(self) -> None:
        self.p: dict[str, float] = {}
        self.delay = 0.0
        self.status = 200
        self.reply: bytes | None = None  # raw body instead of the computed answers
        self.truncate = False  # promise more bytes than are sent, then hang up
        self.requests: list[dict[str, Any]] = []
        self.stopped = threading.Event()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"headers": dict(self.headers), "body": body})
                if fake.delay and fake.stopped.wait(fake.delay):
                    return  # torn down: the client gave up long ago; write nothing
                if fake.status != 200:
                    self.send_error(fake.status)
                    return
                out = (
                    fake.reply if fake.reply is not None else json.dumps(fake.answer(body)).encode()
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out) + 100 * fake.truncate))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self) -> None:  # what urllib turns a followed 303 into
                fake.requests.append({"headers": dict(self.headers), "body": None})
                self.send_error(405)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, body: dict[str, Any]) -> dict[str, Any]:
        facts = body["state"]["facts"]
        return {
            "model": body["model"],
            "answers": {
                q: {"type": "noul", "noul": self.p_for(facts[q])} for q in body["questions"]
            },
            "usage": {"input_tokens": 100, "output_tokens": 0},
        }

    def p_for(self, fact: str) -> float:
        return next((p for s, p in self.p.items() if fact.startswith(s + " ")), 0.0)

    def close(self) -> None:
        self.stopped.set()  # wake any handler still sleeping out a delay
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def jev(monkeypatch):
    fake = FakeJev()
    monkeypatch.setattr(relevance, "ENDPOINT", fake.url)
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    yield fake
    fake.close()


@pytest.fixture()
def no_network(monkeypatch):
    """Every attempt to resolve a name or open a connection, refused and recorded."""
    attempts: list[str] = []

    def refuse(name):
        def guard(*a, **k):
            attempts.append(name)
            raise AssertionError(f"network call on the default path: {name}")

        return guard

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse(f"socket.{name}"))
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, refuse(name))
    return attempts


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "m.db")
    with Store(path) as s:
        for subject, relation, value, when in LEDGER["beliefs"]:
            assert_belief(s, subject, relation, value, valid_from=when)
    return path


def configure(extra: dict[str, Any] | None = None, **settings: object) -> None:
    home = memware_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"relevance": settings, **(extra or {})}))


def hook(capsys, monkeypatch, db: str, prompt: str = PROMPT, **payload: object) -> str:
    """What `memware context --from-hook` prints for a UserPromptSubmit payload."""
    body = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": "s-1"}
    body.update(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(body)))
    capsys.readouterr()
    assert main(["--db", db, "context", "--from-hook"]) == 0
    return capsys.readouterr().out


def injected(out: str) -> list[str]:
    """The belief lines of hook output, in order, without their date."""
    if not out:
        return []
    block = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    return [line.split(" (recorded")[0] for line in block.splitlines()[1:]]


def log_lines() -> list[dict[str, Any]]:
    path = relevance.log_path()
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# -- (a) the default: unchanged bytes, no socket ------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        None,  # no config file at all
        {"relevance": {"mode": "off"}},
        {"relevance": {"mode": "Filtre"}},  # a typo reads as off, never as on
        {"relevance": {"mode": True}},
        {"relevance": "filter"},  # the wrong shape reads as off
        {"relevance": {"threshold": 0.9, "pool": 3}},  # settings without a mode stay off
    ],
)
def test_default_prints_what_origin_main_printed_and_opens_no_socket(
    capsys, monkeypatch, db, no_network, config
):
    """With the key present in the environment, too: a key alone switches nothing on. The golden
    bytes are for a prompt a person typed. The one exception to them is a turn nobody typed,
    which now gets nothing (test_default_injects_nothing_on_a_turn_nobody_typed)."""
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    if config is not None:
        memware_home().mkdir(parents=True, exist_ok=True)
        (memware_home() / "config.json").write_text(json.dumps(config))
    proj = memware_home().parent / "proj"
    proj.mkdir(exist_ok=True)
    assert hook(capsys, monkeypatch, db, cwd=str(proj)) == LEDGER["golden_hook"]
    capsys.readouterr()
    assert main(["--db", db, "context", PROMPT]) == 0
    assert capsys.readouterr().out == LEDGER["golden_plain"]
    assert no_network == []
    assert not relevance.log_path().exists()


NOT_TYPED = [
    (f"<task-notification>\n{PROMPT}\n</task-notification>", {}),
    (f"  \n<task-notification>{PROMPT}</task-notification>", {}),
    (PROMPT, {"agent_id": "a-123", "agent_type": "Explore"}),
]
# How Hermes's notices begin (hermes-agent tools/process_registry_notifications.py and
# gateway/run_notifications.py), each carrying the prompt's words so a keyword search would hit.
HERMES_NOTICES = [
    f"[IMPORTANT: Background process proc_1a2b exited (exit code 0).\nCommand: x\nOutput:\n{PROMPT}]",
    f'[IMPORTANT: Background process proc_1a2b matched watch pattern "502".\n{PROMPT}]',
    f"[IMPORTANT: 3 background processes completed for this session.\n{PROMPT}",
    f"[IMPORTANT: 2 background processes completed. Treat these as one batch.]\n\n{PROMPT}",
    f"[IMPORTANT: Watch patterns disabled for process proc_1a2b — {PROMPT}]",
    f"[IMPORTANT: Watch-pattern overflow: >20 notifications in 60s. {PROMPT}]",
    f"[Background process proc_1a2b heartbeat #3 — still running after 2m.\n{PROMPT}]",
    f"[ASYNC DELEGATION COMPLETE — d_77]\n{PROMPT}",
    f"[ASYNC DELEGATION BATCH COMPLETE — d_77]\n{PROMPT}",
    f"[ASYNC DELEGATION TASK FAILED — d_77, task 1/2]\n{PROMPT}",
]


@pytest.mark.parametrize("prompt, payload", NOT_TYPED + [(n, {}) for n in HERMES_NOTICES])
def test_default_injects_nothing_on_a_turn_nobody_typed(
    capsys, monkeypatch, db, no_network, prompt, payload
):
    """The one change to the default: nobody asked anything on such a turn, so the facts that
    match its words are noise (17 of them across 3 turns of one session, 2026-09-24)."""
    assert injected(hook(capsys, monkeypatch, db)) == injected(LEDGER["golden_hook"])
    assert hook(capsys, monkeypatch, db, prompt, **payload) == ""
    assert no_network == []


@pytest.mark.parametrize(
    "prompt",
    [
        PROMPT,
        f"Why did this arrive? <task-notification>{PROMPT}</task-notification>",
        f"[IMPORTANT] {PROMPT}",
        f"[IMPORTANT: read this first] {PROMPT}",
        f"Background process proc_1a2b exited. {PROMPT}",
        f"[Background process proc_1a2b] {PROMPT}",
        f"Quoting it: [ASYNC DELEGATION COMPLETE — d_77] {PROMPT}",
    ],
)
def test_a_typed_prompt_that_mentions_a_notice_is_still_typed(prompt):
    assert relevance.typed(prompt)
    assert not relevance.typed(prompt, agent=True)


@pytest.mark.parametrize("notice", HERMES_NOTICES)
def test_hermes_prefetch_injects_nothing_on_a_notice(tmp_path, monkeypatch, no_network, notice):
    provider = _hermes(tmp_path)
    assert provider.prefetch(PROMPT) == LEDGER["golden_hermes"]
    assert provider.prefetch(notice) == ""
    assert no_network == []


def test_hermes_prefetch_default_is_what_origin_main_returned(tmp_path, monkeypatch, no_network):
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    provider = _hermes(tmp_path)
    assert provider.prefetch(PROMPT) == LEDGER["golden_hermes"]
    assert no_network == []
    assert not relevance.log_path().exists()


# -- (b) filter mode ------------------------------------------------------------------------


def test_filter_drops_candidates_below_threshold(capsys, monkeypatch, db, jev):
    """The lexical hits go; a relevant fact ranked past k comes in from the wider pool; most
    probable first."""
    configure(mode="filter", threshold=0.5)
    jev.p = {
        "incident report": 0.95,
        "billing api": 0.9,
        "billing worker": 0.8,
        "payments channel": 0.7,
        "short story draft": 0.49,
        "weekly report": 0.3,
    }
    lines = injected(hook(capsys, monkeypatch, db))
    assert lines == [
        "- incident report template: docs/incident-template.md",
        "- billing api owner: the payments team",  # equal probabilities keep memware's order
        "- billing api listens on port: 8443",
        "- billing worker queue: billing-jobs",
        "- billing worker retry limit: 5",
        "- payments channel chat id: C0PAYMENTS",
    ]
    assert "billing api listens on port" not in LEDGER["golden_hook"]  # it came from the pool
    assert len(jev.requests) == 1


def test_filter_caps_at_k_and_can_inject_nothing(capsys, monkeypatch, db, jev):
    configure(mode="filter", threshold=0.5)
    jev.p = dict.fromkeys(RELEVANT, 0.9) | {"billing api": 0.99}
    body = {"hook_event_name": "UserPromptSubmit", "prompt": PROMPT}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(body)))
    capsys.readouterr()
    assert main(["--db", db, "context", "--from-hook", "-k", "3"]) == 0
    lines = injected(capsys.readouterr().out)
    assert len(lines) == 3 and all(x.startswith("- billing api ") for x in lines[:2])

    jev.p = {}
    assert hook(capsys, monkeypatch, db) == ""


def test_request_carries_only_the_prompt_and_the_candidate_facts(capsys, monkeypatch, db, jev):
    """What the README says leaves the machine, and nothing more: no session id, path or date."""
    configure(mode="filter")
    hook(capsys, monkeypatch, db, cwd="/somewhere/private", transcript_path="/somewhere/t.jsonl")
    (req,) = jev.requests
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    body = req["body"]
    assert set(body) == {"state", "model", "questions"}
    assert body["model"] == "jev-1.13.0"
    assert set(body["state"]) == {"prompt", "facts"}
    assert body["state"]["prompt"] == PROMPT
    facts = body["state"]["facts"]
    assert len(facts) == 12  # every belief whose subject the prompt names; not the garden
    assert "billing api listens on port: 8443" in facts.values()
    assert set(body["questions"]) == set(facts)
    assert {q["type"] for q in body["questions"].values()} == {"noul"}
    raw = json.dumps(body)
    for private in ("s-1", "/somewhere", "2026-08", "recorded"):
        assert private not in raw


def test_pool_and_prompt_are_bounded(capsys, monkeypatch, db, jev):
    configure(mode="filter", pool=8)
    hook(capsys, monkeypatch, db, PROMPT + " " + "x" * 20_000)
    state = jev.requests[0]["body"]["state"]
    assert len(state["facts"]) == 8
    assert len(state["prompt"]) <= relevance.MAX_PROMPT_CHARS
    assert state["prompt"].startswith("The billing api") and state["prompt"].endswith("xxx")


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "http 500",
        "http 401",
        "http 429",
        "not json",
        "truncated",
        "no answers",
        "out of range",
    ],
)
def test_filter_fails_open(capsys, monkeypatch, db, jev, failure):
    configure(mode="filter", timeout_s=0.3)
    jev.p = dict.fromkeys(RELEVANT, 0.9)
    if failure == "timeout":
        jev.delay = 2.0
    elif failure.startswith("http"):
        jev.status = int(failure.split()[1])
    elif failure == "not json":
        jev.reply = b"<html>gateway</html>"
    elif failure == "truncated":
        jev.truncate = True  # http.client.IncompleteRead, which is not an OSError
    elif failure == "no answers":
        jev.reply = json.dumps({"model": "jev-1.13.0", "answers": {}}).encode()
    else:
        jev.reply = json.dumps(
            {"answers": {f"f{i}": {"type": "noul", "noul": 1.7} for i in range(12)}}
        ).encode()
    started = time.monotonic()
    out = hook(capsys, monkeypatch, db)
    assert time.monotonic() - started < 1.5  # the deadline holds; the server is still asleep
    assert json.loads(out) == json.loads(LEDGER["golden_hook"])
    assert len(jev.requests) == 1  # one attempt, no retry


def test_the_deadline_covers_a_slow_name_lookup(capsys, monkeypatch, db, jev):
    """The socket timeout bounds each read, not a name lookup; the hook's deadline bounds both."""
    configure(mode="filter", timeout_s=0.3)
    released = threading.Event()

    def slow(
        *a, **k
    ):  # stalls until the test ends, then fails: the abandoned thread connects nowhere
        released.wait(5.0)
        raise OSError("lookup abandoned")

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    try:
        started = time.monotonic()
        out = hook(capsys, monkeypatch, db)
        assert time.monotonic() - started < 1.5
        assert json.loads(out) == json.loads(LEDGER["golden_hook"])
    finally:
        released.set()


def test_filter_fails_open_when_the_endpoint_refuses_the_connection(capsys, monkeypatch, db):
    configure(mode="filter")
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(relevance, "ENDPOINT", f"http://127.0.0.1:{port}/v1/systemone")
    assert json.loads(hook(capsys, monkeypatch, db)) == json.loads(LEDGER["golden_hook"])


def test_filter_without_a_key_sends_nothing(capsys, monkeypatch, db, jev):
    configure(mode="filter")
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert json.loads(hook(capsys, monkeypatch, db)) == json.loads(LEDGER["golden_hook"])
    assert jev.requests == []


def test_key_is_read_from_the_memware_env_file(capsys, monkeypatch, db, jev):
    configure(mode="filter")
    monkeypatch.delenv("TYPESAFE_API_KEY")
    (memware_home() / ".env").write_text("# memware\nTYPESAFE_API_KEY='sk-from-file'\n")
    hook(capsys, monkeypatch, db)
    assert jev.requests[0]["headers"]["Authorization"] == "Bearer sk-from-file"


def test_a_redirect_is_refused_so_the_key_goes_nowhere_else(capsys, monkeypatch, db, jev):
    configure(mode="filter")
    other = FakeJev()
    try:

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.send_response(303)  # urllib follows a 303 as a GET, headers and all
                self.send_header("Location", other.url)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        jev.server.RequestHandlerClass = Redirect
        assert json.loads(hook(capsys, monkeypatch, db)) == json.loads(LEDGER["golden_hook"])
        assert other.requests == []
    finally:
        other.close()


# -- (c) shadow mode ------------------------------------------------------------------------


def test_shadow_injects_unchanged_and_logs_one_line_per_candidate(capsys, monkeypatch, db, jev):
    configure(mode="shadow", threshold=0.5)
    jev.p = dict.fromkeys(RELEVANT, 0.9)
    assert hook(capsys, monkeypatch, db) == LEDGER["golden_hook"]
    rows = log_lines()
    assert len(rows) == len(jev.requests[0]["body"]["state"]["facts"]) == 12
    assert [r["rank"] for r in rows] == list(range(12))
    assert sum(r["today"] for r in rows) == 6  # what memware injected
    chosen = [r["fact"] for r in rows if r["chosen"]]  # what filter mode would have injected
    assert len(chosen) == 6 and all(any(f.startswith(s + " ") for s in RELEVANT) for f in chosen)
    first = rows[0]
    assert first["mode"] == "shadow" and first["harness"] == "claude-code"
    assert first["model"] == "jev-1.13.0" and first["error"] is None
    assert first["prompt"] == PROMPT and first["session"] == "s-1"
    assert all(0.0 <= r["p"] <= 1.0 for r in rows)
    assert len({r["pair_id"] for r in rows}) == 12

    hook(capsys, monkeypatch, db)  # the same prompt again: the same pair ids, to label once
    again = log_lines()[12:]
    assert [r["pair_id"] for r in again] == [r["pair_id"] for r in rows]


def test_shadow_logs_a_failed_call_and_still_injects_unchanged(capsys, monkeypatch, db, jev):
    configure(mode="shadow", timeout_s=0.3)
    jev.delay = 2.0
    assert hook(capsys, monkeypatch, db) == LEDGER["golden_hook"]
    rows = log_lines()
    assert len(rows) == 12
    assert {(r["error"], r["p"], r["chosen"]) for r in rows} == {("timeout", None, None)}


# -- turns nobody typed, and sessions kept out of memware -----------------------------------


@pytest.mark.parametrize("prompt, payload", NOT_TYPED + [(HERMES_NOTICES[0], {})])
@pytest.mark.parametrize("mode", ["off", "shadow", "filter"])
def test_a_turn_nobody_typed_is_never_sent(capsys, monkeypatch, db, jev, prompt, payload, mode):
    """Every mode injects nothing on it, as off does, and none makes a request or logs it."""
    configure(mode=mode)
    jev.p = dict.fromkeys(RELEVANT, 0.9)
    assert hook(capsys, monkeypatch, db, prompt, **payload) == ""
    assert jev.requests == [] and log_lines() == []


@pytest.mark.parametrize("how", ["no-capture env", "no-capture list", "capture.exclude", "marker"])
@pytest.mark.parametrize("mode", ["shadow", "filter"])
def test_a_session_memware_keeps_out_is_never_sent(
    capsys, monkeypatch, db, jev, tmp_path, how, mode
):
    """What memware would not index, it does not send: the session gets what off injects."""
    transcript = tmp_path / "projects" / "-Users-me-work-client" / "s-9.jsonl"
    transcript.parent.mkdir(parents=True)
    extra: dict[str, Any] = {}
    prompt = PROMPT
    if how == "no-capture env":
        monkeypatch.setenv("MEMWARE_NO_CAPTURE", "1")
    elif how == "no-capture list":
        memware_home().mkdir(parents=True, exist_ok=True)
        (memware_home() / "no-capture.txt").write_text(f"{transcript.resolve()}\n")
    elif how == "capture.exclude":
        extra = {"capture": {"exclude": ["*/-Users-me-work-*/*"]}}
    else:
        monkeypatch.setenv("MEMWARE_IGNORE_MARKERS", "[memware-eval]")
        prompt = PROMPT + " [memware-eval]"
    configure(extra, mode=mode)
    jev.p = dict.fromkeys(RELEVANT, 0.9)
    out = hook(capsys, monkeypatch, db, prompt, transcript_path=str(transcript))
    assert injected(out) == injected(LEDGER["golden_hook"])
    assert jev.requests == [] and log_lines() == []


def test_key_never_reaches_the_log_the_usage_file_or_stderr(capsys, monkeypatch, db, jev):
    configure(mode="shadow", timeout_s=0.3)
    jev.p = dict.fromkeys(RELEVANT, 0.9)
    hook(capsys, monkeypatch, db)
    jev.status = 401
    hook(capsys, monkeypatch, db)
    jev.status, jev.delay = 200, 2.0
    hook(capsys, monkeypatch, db)
    capsys.readouterr()
    assert main(["config", "relevance.mode", "filter"]) == 0
    captured = capsys.readouterr()
    assert {r["error"] for r in log_lines()} == {None, "http 401", "timeout"}
    written = relevance.log_path().read_text() + relevance.usage_path().read_text()
    assert KEY not in written + captured.out + captured.err


def test_an_answered_request_writes_one_usage_line(capsys, monkeypatch, db, jev):
    configure(mode="filter", timeout_s=0.3)
    hook(capsys, monkeypatch, db)
    jev.delay = 2.0
    hook(capsys, monkeypatch, db)  # no answer, so no usage line
    (line,) = [json.loads(x) for x in relevance.usage_path().read_text().splitlines()]
    assert line["process"] == "memware-relevance" and line["model"] == "jev-1.13.0"
    assert line["input_tokens"] == 100 and line["cost_usd"] == round(100 * 0.042e-6, 6)
    assert isinstance(line["latency_ms"], int) and "prompt" not in line


# -- memware config ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, value",
    [
        ("relevance.mode", "on"),
        ("relevance.threshold", "1.5"),
        ("relevance.threshold", "high"),
        ("relevance.pool", "0"),
        ("relevance.pool", "2.5"),
        ("relevance.timeout_s", "30"),
        ("relevance.model", " "),
        ("relevance.endpoint", "https://example.test"),
    ],
)
def test_config_refuses_a_bad_relevance_value(capsys, key, value):
    assert main(["config", key, value]) == 2
    assert "nothing written" in capsys.readouterr().err
    assert not (memware_home() / "config.json").exists()


def test_config_writes_typed_values_and_says_what_turning_it_on_sends(capsys, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert main(["config", "relevance.threshold", "0.6"]) == 0
    assert main(["config", "relevance.pool", "12"]) == 0
    capsys.readouterr()
    assert main(["config", "relevance.mode", "shadow"]) == 0
    err = capsys.readouterr().err
    assert "api.typesafe.ai" in err and "TYPESAFE_API_KEY" in err
    written = json.loads((memware_home() / "config.json").read_text())
    assert written == {"relevance": {"threshold": 0.6, "pool": 12, "mode": "shadow"}}
    settings = relevance.settings()
    assert (settings.mode, settings.threshold, settings.pool) == ("shadow", 0.6, 12)
    assert (settings.model, settings.timeout_s) == ("jev-1.13.0", 1.5)


# -- Hermes prefetch ----------------------------------------------------------------------------


def _hermes(tmp_path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("memware_hermes_relevance", HERMES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "hermes").mkdir()
    (tmp_path / "hermes" / "memware.json").write_text(
        json.dumps({"db_path": str(tmp_path / "h.db")})
    )
    provider = mod.MemwareProvider()
    provider.initialize("sess-h", hermes_home=str(tmp_path / "hermes"))
    with Store(provider._db) as s:
        for subject, relation, value, when in LEDGER["beliefs"]:
            assert_belief(s, subject, relation, value, valid_from=when)
    return provider


def test_hermes_prefetch_filters_and_logs(tmp_path, jev):
    configure(mode="filter")
    jev.p = dict.fromkeys(RELEVANT, 0.9)
    block = _hermes(tmp_path).prefetch(PROMPT)
    assert "short story draft" not in block and "billing api listens on port: 8443" in block
    rows = log_lines()
    assert {r["harness"] for r in rows} == {"hermes"} and rows[0]["session"] == "sess-h"


def test_hermes_prefetch_fails_open(tmp_path, jev):
    configure(mode="filter", timeout_s=0.3)
    jev.delay = 2.0
    assert _hermes(tmp_path).prefetch(PROMPT) == LEDGER["golden_hermes"]


def test_hermes_shadow_returns_what_off_returns(tmp_path, jev):
    configure(mode="shadow")
    jev.p = dict.fromkeys(RELEVANT, 0.9)
    assert _hermes(tmp_path).prefetch(PROMPT) == LEDGER["golden_hermes"]
    assert len(log_lines()) == 12


@pytest.mark.parametrize("mode", ["shadow", "filter"])
def test_hermes_never_sends_a_notice(tmp_path, jev, mode):
    configure(mode=mode)
    jev.p = dict.fromkeys(RELEVANT, 0.9)
    assert _hermes(tmp_path).prefetch(HERMES_NOTICES[0]) == ""
    assert jev.requests == [] and log_lines() == []
