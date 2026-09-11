"""``memware derive`` — the transcript → belief pass.

The provider is stubbed almost everywhere: these pin the DETERMINISTIC half, which is the
half that decides what reaches the ledger. The model can only ever propose. The Claude Code
provider is exercised against a fake ``claude`` on PATH, so its envelope parsing, its
subscription-only environment and its usage-limit exit are real.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from memware import derive as md
from memware.cli import main
from memware.store import Store

TURNS = [
    (1, "s-old", 1, "2026-08-01T10:00:00Z", "user", "The engine is pinned at 0.11.0."),
    (2, "s-old", 2, "2026-08-01T10:01:00Z", "assistant", "Understood."),
    (
        3,
        "s-new",
        1,
        "2026-09-01T09:00:00Z",
        "user",
        "We switched to deepseek-v4-flash. The proxy lives at 127.0.0.1:3119.",
    ),
    (4, "s-new", 2, "2026-09-01T09:05:00Z", "assistant", "Nothing notable here."),
    (5, "s-tool", 1, "2026-09-01T09:10:00Z", "tool", "The tool output is now large."),
]


@pytest.fixture()
def db(tmp_path: Path) -> str:
    """A real store (WAL, migrated) with three sessions; ids ascend so a watermark can cut."""
    path = tmp_path / "scratch.db"
    with Store(path) as s:
        s.conn.executemany(
            "INSERT INTO turn(id,session,seq,ts,role,text,source,harness) "
            "VALUES (?,?,?,?,?,?,?,'claude-code')",
            [(*row, f"f{row[0]}.jsonl") for row in TURNS],
        )
        s.conn.commit()
    return str(path)


class StubProvider:
    """Returns canned model output; records what it was asked."""

    name = "stub"
    chunk = 8

    def __init__(self, answers):
        self.answers = answers
        self.prompts = []
        self.usage = md.Usage()

    def describe(self):
        return "stub"

    def complete(self, system, user, timeout=120):
        self.prompts.append(user)
        self.usage.calls += 1
        return json.dumps(self.answers.pop(0))


def stub(monkeypatch, answers, **attrs):
    prov = StubProvider(answers)
    for k, v in attrs.items():
        setattr(prov, k, v)
    monkeypatch.setattr(md, "make_provider", lambda name, env, model=None: prov)
    return prov


KEEP_ENGINE = {
    "n": 1,
    "anchor": "The engine is pinned at 0",
    "keep": True,
    "subject": "the engine",
    "relation": "pinned version",
    "value": "0.11.0",
}
KEEP_PROXY = {
    "n": 2,
    "anchor": "We switched to deepseek v4 flash",
    "keep": True,
    "subject": "the proxy",
    "relation": "address",
    "value": "127.0.0.1:3119",
}


# ── evidence is never rewritten ─────────────────────────────────────────
def test_source_db_is_opened_read_only(db):
    conn = md.open_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE turn SET text='rewritten' WHERE id=1")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM turn WHERE id=1")
    assert conn.execute("SELECT text FROM turn WHERE id=1").fetchone()[0].endswith("0.11.0.")
    conn.close()


def test_open_readonly_works_without_wal_side_files(db):
    """A WAL store between writers has no -wal/-shm; a mode=ro URI open fails there on
    some SQLite builds. query_only does not."""
    for f in (db + "-wal", db + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    conn = md.open_readonly(db)
    assert conn.execute("SELECT count(*) FROM turn").fetchone()[0] == 5
    conn.close()


def test_open_readonly_refuses_a_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        md.open_readonly(str(tmp_path / "nope.db"))


# ── only sessions with new turns ────────────────────────────────────────
def test_sessions_with_new_turns_respects_the_watermark(db):
    conn = md.open_readonly(db)
    assert md.sessions_with_new_turns(conn, 0) == ["s-old", "s-new"]
    assert md.sessions_with_new_turns(conn, 2) == ["s-new"]
    assert md.sessions_with_new_turns(conn, 5) == []
    assert "s-tool" not in md.sessions_with_new_turns(conn, 0)
    assert md.max_turn_id(conn) == 4  # the tool turn is not evidence
    assert [r["id"] for r in md.new_turns(conn, "s-new", 3)] == [4]
    conn.close()


# ── the watermark ───────────────────────────────────────────────────────
def test_state_lives_beside_its_own_database(tmp_path):
    assert md.state_path("~/.memware/memware.db").name == "memware.db.derive.json"
    assert md.state_path(str(tmp_path / "scratch.db")).parent == tmp_path


def test_state_round_trips_and_tolerates_junk(tmp_path):
    p = tmp_path / "s.json"
    assert md.load_state(p)["watermark"] == 0
    md.save_state(p, {"watermark": 41, "runs": 2, "last_run": "2026-09-03T00:00:00Z"})
    assert md.load_state(p)["watermark"] == 41
    p.write_text("{not json")
    assert md.load_state(p)["watermark"] == 0


# ── the deterministic backstop ──────────────────────────────────────────
REGION = (
    "We switched the deriver to deepseek-v4-flash. "
    "The proxy lives at 127.0.0.1:3119 and answers on /v1."
)


def _item(**kw):
    base = {
        "n": 1,
        "anchor": "We switched the deriver to deepseek",
        "keep": True,
        "subject": "the deriver",
        "relation": "model",
        "value": "deepseek-v4-flash",
    }
    base.update(kw)
    return base


def test_a_grounded_triple_is_admitted():
    cand, why = md.validate(_item(), REGION)
    assert why is None
    assert cand == {"subject": "the deriver", "relation": "model", "value": "deepseek-v4-flash"}


@pytest.mark.parametrize(
    "bad, reason_fragment",
    [
        (_item(value="deepseek-v4-flash-20260901"), "NOT GROUNDED"),  # a hallucinated version
        (_item(value="gemini-3.1-flash-lite"), "NOT GROUNDED"),
        (_item(value="run the tests then report"), "task instruction"),
        (_item(subject="it"), "deictic"),
        (_item(value="this"), "deictic"),
        (_item(subject=""), "incomplete"),
        (_item(relation="is the model that we now use for the deriver"), "relation too long"),
        (_item(subject="x" * 200), "subject too long"),
        (_item(keep=False), "model rejected"),
        (_item(anchor="Something else entirely about backups now"), "MISALIGNED"),
    ],
)
def test_the_backstop_refuses(bad, reason_fragment):
    cand, why = md.validate(bad, REGION)
    assert cand is None
    assert reason_fragment in why


def test_grounded_ignores_function_words():
    assert md.grounded("deepseek-v4-flash", REGION)
    assert md.grounded("the proxy is at 127.0.0.1:3119", REGION)
    assert not md.grounded("127.0.0.1:3120", REGION)
    assert not md.grounded("", REGION)


def test_source_pointer_and_event_time():
    assert md.source_pointer("abc-123", 77) == "memware:session/abc-123/turn/77"
    assert md.valid_from_of({"ts": "2026-09-01T09:00:00.123Z"}) == "2026-09-01T09:00:00Z"
    assert md.valid_from_of({"ts": ""}) is None
    assert md.valid_from_of({"ts": "not a timestamp"}) is None


@pytest.mark.parametrize(
    "text, want",
    [
        ("Never ship to the U.S. without a compliance review.", 1),
        ("Always flush the buffer (e.g. before a fork) or you lose writes.", 1),
        ("The build passed. Never merge without a review. We shipped.", 3),
    ],
)
def test_split_sentences_respects_abbreviations(text, want):
    assert len(md.split_sentences(text)) == want


def test_region_keeps_the_neighbouring_sentence():
    text = "The proxy moved last night. It is now on 127.0.0.1:3119. Everyone should update."
    regions = md.regions_from_texts([text])
    assert (
        regions and "The proxy moved last night." in regions[0] and "127.0.0.1:3119" in regions[0]
    )


def test_parse_model_json_strips_fences_and_salvages():
    assert md.parse_model_json('```json\n[{"n": 1}]\n```') == [{"n": 1}]
    assert [o["n"] for o in md.parse_model_json('[{"n": 1, "keep": true},, {"n": 2}]')] == [1, 2]
    with pytest.raises(ValueError):
        md.parse_model_json("I could not find any facts in these excerpts.")


def test_reliability_stays_below_a_human_stated_belief():
    """0.5 is what makes gate_conflicts route a challenge to review."""
    assert md.RELIABILITY == 0.5
    assert md.POLICY.value == "gate_conflicts"


# ── batching: regions from every session go to the provider in provider-sized chunks ──
def test_regions_are_batched_across_sessions_by_the_providers_chunk(db, monkeypatch):
    prov = stub(monkeypatch, [[KEEP_ENGINE, KEEP_PROXY]], chunk=24)
    rc = main(["--db", db, "derive"])
    assert rc == 0
    assert prov.usage.calls == 1, "two sessions' excerpts fit one call at chunk 24"
    assert "[1] " in prov.prompts[0] and "[2] " in prov.prompts[0]


def test_small_chunks_mean_more_calls(db, monkeypatch):
    prov = stub(monkeypatch, [[KEEP_ENGINE], [{**KEEP_PROXY, "n": 2}]], chunk=1)
    assert main(["--db", db, "derive"]) == 0
    assert prov.usage.calls == 2


# ── dry run, apply, and the watermark ───────────────────────────────────
def test_dry_run_writes_nothing_and_does_not_advance(db, tmp_path, monkeypatch, capsys):
    stub(monkeypatch, [[KEEP_ENGINE, KEEP_PROXY]], chunk=24)
    state = tmp_path / "state.json"
    assert main(["--db", db, "derive", "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert not state.exists()
    assert (
        "dry run" in out and "NOT advanced" in out and "the engine | pinned version = 0.11.0" in out
    )
    with Store(db) as s:
        assert s.conn.execute("SELECT count(*) FROM belief").fetchone()[0] == 0


def test_apply_writes_through_the_ledger_and_advances_the_watermark(
    db, tmp_path, monkeypatch, capsys
):
    stub(monkeypatch, [[KEEP_ENGINE, KEEP_PROXY]], chunk=24)
    state = tmp_path / "state.json"
    assert main(["--db", db, "derive", "--state", str(state), "--apply"]) == 0
    assert json.loads(state.read_text())["watermark"] == 4
    assert "created=2" in capsys.readouterr().out
    with Store(db) as s:
        rows = s.conn.execute(
            "SELECT subject, relation, value, source, reliability, valid_from FROM belief ORDER BY id"
        ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("the engine", "pinned version", "0.11.0"),
        ("the proxy", "address", "127.0.0.1:3119"),
    ]
    assert rows[0][3] == "memware:session/s-old/turn/1" and rows[0][4] == 0.5
    assert rows[0][5].startswith("2026-08-01T10:00:00")
    # a second run has nothing new and must cost nothing
    prov = stub(monkeypatch, [])
    assert main(["--db", db, "derive", "--state", str(state), "--apply"]) == 0
    assert prov.usage.calls == 0


def test_a_provider_failure_writes_nothing(db, tmp_path, monkeypatch):
    class Dead(StubProvider):
        def complete(self, system, user, timeout=120):
            raise RuntimeError("endpoint down")

    monkeypatch.setattr(md, "make_provider", lambda name, env, model=None: Dead([]))
    monkeypatch.setattr(md.time, "sleep", lambda *_: None)
    state = tmp_path / "state.json"
    assert main(["--db", db, "derive", "--state", str(state), "--apply"]) == 0
    with Store(db) as s:
        assert s.conn.execute("SELECT count(*) FROM belief").fetchone()[0] == 0


def test_if_stale_skips_a_recent_run_and_auto_needs_the_config(db, tmp_path, monkeypatch, capsys):
    state = tmp_path / "state.json"
    md.save_state(
        state,
        {"watermark": 0, "runs": 1, "last_run": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    )
    prov = stub(monkeypatch, [[KEEP_ENGINE, KEEP_PROXY]], chunk=24)
    assert main(["--db", db, "derive", "--state", str(state), "--if-stale", "24"]) == 0
    assert "skipped" in capsys.readouterr().out and prov.usage.calls == 0
    old = tmp_path / "old.json"
    md.save_state(old, {"watermark": 0, "runs": 1, "last_run": "2026-01-01T00:00:00Z"})
    # --auto without `derive.auto` on: silent no-op even though the run is stale
    assert (
        main(["--db", db, "derive", "--state", str(old), "--if-stale", "24", "--auto", "--quiet"])
        == 0
    )
    assert prov.usage.calls == 0
    assert main(["--db", db, "config", "derive.auto", "true"]) == 0
    assert (
        main(["--db", db, "derive", "--state", str(old), "--if-stale", "24", "--auto", "--quiet"])
        == 0
    )
    assert prov.usage.calls == 1


# ── providers ───────────────────────────────────────────────────────────
def test_openai_provider_needs_its_three_settings(db, tmp_path, monkeypatch, capsys):
    for k in md.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    rc = main(["--db", db, "derive", "--provider", "openai"])
    assert rc == md.EXIT_CONFIG
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_openai_rejected_credential_exits_4_after_one_call(db, tmp_path, monkeypatch, capsys):
    import urllib.error

    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("OPENAI_API_KEY", "stale")
    calls = []

    def refuse(req, timeout=0):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(md.urllib.request, "urlopen", refuse)
    state = tmp_path / "state.json"
    rc = main(["--db", db, "derive", "--provider", "openai", "--state", str(state), "--apply"])
    assert rc == md.EXIT_UNAVAILABLE
    assert len(calls) == 1, "one call, not one per chunk with a retry each"
    assert "watermark not advanced" in capsys.readouterr().err
    assert not state.exists()


def fake_claude(tmp_path: Path, monkeypatch, body: str) -> Path:
    """A `claude` on PATH that records its argv/env and prints a canned envelope."""
    log = tmp_path / "claude-calls.jsonl"
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        f"printf 'KEY=%s\\n' \"${{ANTHROPIC_API_KEY-unset}}\" >> '{log}'\n"
        f"printf 'THINK=%s\\n' \"${{MAX_THINKING_TOKENS-unset}}\" >> '{log}'\n"
        f"cat <<'EOF'\n{body}\nEOF\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    return log


def test_claude_code_provider_runs_on_the_subscription(db, tmp_path, monkeypatch, capsys):
    envelope = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": json.dumps([KEEP_ENGINE, KEEP_PROXY]),
            "usage": {"input_tokens": 900, "output_tokens": 60},
        }
    )
    log = fake_claude(tmp_path, monkeypatch, envelope)
    state = tmp_path / "state.json"
    assert main(["--db", db, "derive", "--state", str(state), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "haiku via claude -p (subscription)" in out and "created=2" in out
    calls = log.read_text()
    assert "--model haiku --output-format json --system-prompt" in calls
    assert "--tools  --no-session-persistence --exclude-dynamic-system-prompt-sections" in calls
    assert "--bare" not in calls, "--bare skips the subscription login"
    assert "KEY=unset" in calls, "the API key must not reach claude -p"
    assert "THINK=0" in calls, "thinking is switched off for the extraction"
    assert calls.count("KEY=") == 1, "both sessions' excerpts went in one spawn"


def test_claude_code_usage_limit_exits_4(db, tmp_path, monkeypatch, capsys):
    envelope = json.dumps(
        {"type": "result", "is_error": True, "result": "You've hit your session limit · resets 6pm"}
    )
    fake_claude(tmp_path, monkeypatch, envelope)
    state = tmp_path / "state.json"
    rc = main(["--db", db, "derive", "--state", str(state), "--apply"])
    assert rc == md.EXIT_UNAVAILABLE
    assert "usage limit" in capsys.readouterr().err
    assert not state.exists()


def test_missing_claude_binary_is_a_config_error(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on it
    rc = main(["--db", db, "derive"])
    assert rc == md.EXIT_CONFIG
    assert "claude" in capsys.readouterr().err


def test_env_file_supplies_openai_settings_and_process_env_wins(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('OPENAI_BASE_URL="http://x/v1"\nOPENAI_MODEL=from-file\nOPENAI_API_KEY=k\n')
    for k in md.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    env = md.read_env([f])
    assert env["OPENAI_BASE_URL"] == "http://x/v1" and env["OPENAI_MODEL"] == "from-file"
    monkeypatch.setenv("MEMWARE_DERIVE_MODEL", "from-env")
    assert md.OpenAIProvider(md.read_env([f])).model == "from-env"


# ── one derive at a time ────────────────────────────────────────────────
def test_a_live_lock_skips_and_a_stale_lock_is_taken_over(db, tmp_path, monkeypatch, capsys):
    """Two session-start hooks firing together must not derive the same turns twice."""
    prov = stub(monkeypatch, [[KEEP_ENGINE, KEEP_PROXY]], chunk=24)
    state = tmp_path / "state.json"
    lock = tmp_path / "state.json.lock"
    lock.write_text(str(os.getpid()))  # a live holder
    assert main(["--db", db, "derive", "--state", str(state), "--apply"]) == 0
    assert "another derive is running" in capsys.readouterr().out
    assert prov.usage.calls == 0 and not state.exists()
    lock.write_text("999999999")  # a dead holder: taken over
    assert main(["--db", db, "derive", "--state", str(state), "--apply"]) == 0
    assert prov.usage.calls == 1 and state.exists() and not lock.exists()


# ── --plan: what would leave the machine, without it leaving ───────────
ROOT = Path(__file__).resolve().parents[1]
ENGINE = "The engine is pinned at 0.11.0."
PROXY = "We switched to deepseek-v4-flash. The proxy lives at 127.0.0.1:3119."


def forbid_network(monkeypatch):
    """subprocess.run and urlopen both raise AND record: ``_call_chunk`` swallows a chunk's
    exception and retries, so a raise alone would not fail a run that leaked."""
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("--plan reached for the network")

    monkeypatch.setattr(md.subprocess, "run", refuse)
    monkeypatch.setattr(md.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(md.time, "sleep", lambda *_: None)
    return calls


def forbid_provider(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("--plan built a provider")

    monkeypatch.setattr(md, "make_provider", refuse)


def test_plan_prints_every_excerpt_without_claude_on_path(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))  # no `claude`
    for k in md.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    state = tmp_path / "state.json"
    assert main(["--db", db, "derive", "--state", str(state), "--plan"]) == 0
    out = capsys.readouterr().out
    assert f"[1] {ENGINE}\n      session s-old  source memware:session/s-old/turn/1" in out
    assert f"[2] {PROXY}\n      session s-new  source memware:session/s-new/turn/3" in out
    assert "sessions    : 2 with new turns" in out
    assert f"excerpts    : 2  ({len(ENGINE) + len(PROXY)} chars)" in out
    assert "model calls : 1  (2 excerpts / chunk 24)" in out
    assert "destination : claude-code: haiku via claude -p (subscription)" in out
    assert "`claude` is not on PATH — a run would exit 2" in out
    assert not state.exists()


@pytest.mark.parametrize(
    "provider, where",
    [
        ("claude-code", "claude-code: haiku via claude -p (subscription)"),
        ("openai", "openai: small-model @ https://llm.example.com/v1"),
    ],
)
def test_plan_makes_no_provider_call(db, tmp_path, monkeypatch, capsys, provider, where):
    """Both providers are fully set up, so nothing but --plan stands between the excerpts and
    the network."""
    for k in md.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    fake_claude(tmp_path, monkeypatch, json.dumps({"result": json.dumps([KEEP_ENGINE])}))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "small-model")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    calls = forbid_network(monkeypatch)
    argv = ["--db", db, "derive", "--state", str(tmp_path / "s.json"), "--provider", provider]
    assert main([*argv, "--plan"]) == 0
    out = capsys.readouterr().out
    assert calls == [], "--plan called the provider"
    assert f"destination : {where}" in out and "not ready" not in out
    # the guard bites: the same command without --plan reaches for the network
    main(argv)
    assert calls


def test_plan_writes_no_state_and_ignores_the_run_lock(db, tmp_path, monkeypatch, capsys):
    forbid_provider(monkeypatch)
    state = tmp_path / "state.json"
    md.save_state(state, {"watermark": 2, "runs": 3, "last_run": "2026-09-01T00:00:00Z"})
    lock = tmp_path / "state.json.lock"
    lock.write_text(str(os.getpid()))  # a derive is running: a read-only view need not wait
    before = (state.read_bytes(), state.stat().st_mtime_ns, lock.read_bytes())
    assert main(["--db", db, "derive", "--state", str(state), "--plan"]) == 0
    out = capsys.readouterr().out
    assert (state.read_bytes(), state.stat().st_mtime_ns, lock.read_bytes()) == before
    assert "watermark   : 2 -> 4" in out and "s-old" not in out and "[1] We switched" in out
    assert "NOT advanced" in out
    lock.unlink()
    assert main(["--db", db, "derive", "--state", str(state), "--plan"]) == 0
    assert not lock.exists(), "--plan took the run lock"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("state")) == [
        "state.json"
    ]


def test_plan_honours_the_run_flags(db, tmp_path, monkeypatch, capsys):
    forbid_provider(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path))
    for k in md.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    base = ["--db", db, "derive", "--state", str(tmp_path / "state.json"), "--plan"]

    assert main([*base, "--since", "2"]) == 0
    out = capsys.readouterr().out
    assert "s-old" not in out and "sessions    : 1 with new turns\n" in out

    assert main([*base, "--max-sessions", "1"]) == 0
    out = capsys.readouterr().out
    assert "s-new" not in out and "sessions    : 1 with new turns (capped)" in out

    assert main([*base, "--since", "4"]) == 0
    out = capsys.readouterr().out
    assert "excerpts    : 0  (0 chars)" in out and "model calls : 0" in out

    assert main([*base, "--chunk", "1", "--model", "sonnet"]) == 0
    out = capsys.readouterr().out
    assert "model calls : 2  (2 excerpts / chunk 1)" in out
    assert "destination : claude-code: sonnet via claude -p (subscription)" in out

    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.com/v1/")
    assert main([*base, "--provider", "openai", "--model", "big-model"]) == 0
    out = capsys.readouterr().out
    assert "model calls : 1  (2 excerpts / chunk 8)" in out
    assert "destination : openai: big-model @ https://llm.example.com/v1\n" in out
    assert "not ready   : OPENAI_API_KEY not set — a run would exit 2" in out

    assert main([*base, "--quiet"]) == 0
    labels = [ln.split(" : ")[0].strip() for ln in capsys.readouterr().out.splitlines()]
    assert labels == ["sessions", "excerpts", "model calls", "destination", "not ready"]


def test_plan_lists_exactly_what_a_run_sends(db, monkeypatch, capsys):
    prov = stub(monkeypatch, [[KEEP_ENGINE, KEEP_PROXY]], chunk=24)
    assert main(["--db", db, "derive"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "derive", "--plan"]) == 0
    listed = [ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.startswith("  [")]
    assert "\n\n".join(listed) == prov.prompts[0]


def test_plan_and_apply_are_exclusive_and_chunk_is_positive(db, capsys):
    for argv in (["--plan", "--apply"], ["--plan", "--chunk", "0"], ["--plan", "--chunk", "x"]):
        with pytest.raises(SystemExit) as e:
            main(["--db", db, "derive", *argv])
        assert e.value.code == 2
    err = capsys.readouterr().err
    assert "not allowed with argument" in err and "must be at least 1" in err
    assert "expected a whole number" in err


def test_plan_refuses_an_unknown_provider_like_a_run(db, monkeypatch, capsys):
    """No destination to describe: the plan fails the way the run would, exit 2."""
    monkeypatch.setenv("MEMWARE_DERIVE_PROVIDER", "carrier-pigeon")
    assert main(["--db", db, "derive", "--plan"]) == md.EXIT_CONFIG
    assert "unknown provider 'carrier-pigeon'" in capsys.readouterr().err


def test_a_dry_run_still_sends_the_excerpts_to_the_provider(db, tmp_path, monkeypatch, capsys):
    """Pinned so the docs and the code cannot drift apart again: a dry run skips the write and
    the watermark, not the model call."""
    envelope = json.dumps(
        {"type": "result", "is_error": False, "result": json.dumps([KEEP_ENGINE, KEEP_PROXY])}
    )
    log = fake_claude(tmp_path, monkeypatch, envelope)
    state = tmp_path / "state.json"
    assert main(["--db", db, "derive", "--state", str(state)]) == 0
    sent = log.read_text()
    assert ENGINE in sent and PROXY in sent
    assert "dry run (excerpts go to the model, no writes)" in capsys.readouterr().out
    assert not state.exists()
    with Store(db) as s:
        assert s.conn.execute("SELECT count(*) FROM belief").fetchone()[0] == 0


def test_the_docs_say_a_dry_run_sends_and_point_at_plan(capsys):
    with pytest.raises(SystemExit):
        main(["derive", "--help"])
    texts = {
        "--help": capsys.readouterr().out,
        "README.md": (ROOT / "README.md").read_text(),
        "docs/scheduling.md": (ROOT / "docs" / "scheduling.md").read_text(),
    }
    for name, text in texts.items():
        flat = " ".join(text.split())
        assert "--plan" in flat, name
        mentions = list(re.finditer(r"dry run", flat, re.I))
        assert mentions, name
        for m in mentions:
            assert "send" in flat[m.end() : m.end() + 40], (
                f"{name}: {flat[m.start() : m.end() + 60]!r}"
            )
