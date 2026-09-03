"""memware memory provider — wiring contract.

The ``memware`` package is not a core dependency, so these tests stub it the
way ``test_memory_lazy_install.py`` stubs the supermemory/mem0 SDKs: they
exercise the provider's own logic (lazy-install chokepoint, config resolution,
profile scoping, capture, tool routing) without touching PyPI or writing a
real store.

The two contracts most easily broken are pinned first: ``is_available()`` must
not gate on the package being importable (the sealed-venv chicken-and-egg that
stopped supermemory loading at all), and ``memory.memware`` must be in the
``LAZY_DEPS`` allowlist or ``ensure()`` raises instead of installing.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
import tools.lazy_deps as ld
from plugins.memory.memware import MemwareMemoryProvider

# ---------------------------------------------------------------------------
# Stub backend
# ---------------------------------------------------------------------------


class _FakeStore:
    """Stands in for memware.store.Store — a context manager over a path."""

    def __init__(self, path):
        self.path = str(path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self):
        return None


class _Hit:
    def __init__(self, subject, relation, value, ts="2026-01-02T03:04:05Z"):
        self.id = 1
        self.kind = "belief"
        self.text = f"{subject} {relation} {value}"
        self.session = None
        self.ts = ts
        self.role = None
        self.subject = subject
        self.relation = relation
        self.snippet = None


@pytest.fixture()
def backend(monkeypatch):
    """Install fake ``memware.*`` modules and record every call made to them."""
    calls: dict = {"beliefs": [], "synced": [], "ensure": [], "hits": []}

    store_mod = types.ModuleType("memware.store")
    store_mod.Store = _FakeStore
    store_mod.now_iso = lambda: "2026-01-02T03:04:05Z"

    index_mod = types.ModuleType("memware.index")
    index_mod.search_beliefs = lambda store, query, **kw: list(calls["hits"])
    index_mod.search_turns = lambda store, query, **kw: []
    index_mod.read_turns = lambda store, session, **kw: [{"session": session}]

    ledger_mod = types.ModuleType("memware.ledger")
    ledger_mod.Policy = types.SimpleNamespace(GATE_CONFLICTS="gate_conflicts")

    def _assert_belief(store, subject, relation, value, **kw):
        calls["beliefs"].append({"subject": subject, "relation": relation, "value": value, **kw})
        return types.SimpleNamespace(
            outcome=types.SimpleNamespace(value="committed"), belief_id=7, review_id=None
        )

    ledger_mod.assert_belief = _assert_belief
    ledger_mod.current = lambda store, subject=None: [{"subject": subject or "all"}]

    ingest_mod = types.ModuleType("memware.ingest")
    ingest_mod.sync_file = (
        lambda store, path, *, harness: calls["synced"].append((str(path), harness)) or 0
    )

    root = types.ModuleType("memware")
    for name, mod in {
        "memware": root,
        "memware.store": store_mod,
        "memware.index": index_mod,
        "memware.ledger": ledger_mod,
        "memware.ingest": ingest_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    monkeypatch.setattr(ld, "ensure", lambda feature, **kw: calls["ensure"].append((feature, kw)))
    return calls


@pytest.fixture()
def provider(backend, tmp_path, monkeypatch):
    """An initialized provider whose HERMES_HOME is ``tmp_path``."""
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    prov = MemwareMemoryProvider()
    prov.initialize("sess-1", hermes_home=str(tmp_path))
    return prov


# ---------------------------------------------------------------------------
# 1. Availability and the lazy-install allowlist
# ---------------------------------------------------------------------------


def test_available_even_when_package_absent(monkeypatch):
    """Gating availability on the import would stop the provider loading on a
    sealed venv, so initialize() — which installs the package — never runs."""
    import builtins

    real_import = builtins.__import__

    def _no_memware(name, *args, **kwargs):
        if name == "memware" or name.startswith("memware."):
            raise ImportError("No module named 'memware'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_memware)
    assert MemwareMemoryProvider().is_available() is True


def test_feature_is_allowlisted():
    assert "memory.memware" in ld.LAZY_DEPS
    assert any(ld._pkg_name_from_spec(s) == "memware" for s in ld.LAZY_DEPS["memory.memware"])


def test_initialize_calls_ensure_before_importing(provider, backend):
    assert ("memory.memware", {"prompt": False}) in backend["ensure"]


# ---------------------------------------------------------------------------
# 2. Config resolution and profile scoping
# ---------------------------------------------------------------------------


def test_default_db_is_profile_scoped(provider, tmp_path):
    assert Path(provider._db) == tmp_path / "memware" / "memware.db"


def test_db_path_expands_hermes_home_and_tilde(backend, tmp_path, monkeypatch):
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    (tmp_path / "memware.json").write_text(
        json.dumps({"db_path": "$HERMES_HOME/custom.db", "prefetch_k": 2, "auto_sync": False})
    )
    prov = MemwareMemoryProvider()
    prov.initialize("sess-1", hermes_home=str(tmp_path))

    assert Path(prov._db) == tmp_path / "custom.db"
    assert prov._prefetch_k == 2 and prov._auto_sync is False


def test_initialize_falls_back_to_get_hermes_home(backend, tmp_path, monkeypatch):
    """hermes_home is normally passed in; without it the active profile wins."""
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    prov = MemwareMemoryProvider()
    prov.initialize("sess-1")

    assert Path(prov._db) == tmp_path / "memware" / "memware.db"


def test_save_config_coerces_types_and_keeps_unknown_keys(provider, tmp_path):
    (tmp_path / "memware.json").write_text(json.dumps({"kept": "value"}))
    provider.save_config(
        {"db_path": "~/shared.db", "prefetch_k": "3", "auto_sync": "false"}, str(tmp_path)
    )

    saved = json.loads((tmp_path / "memware.json").read_text())
    assert saved == {
        "kept": "value",
        "db_path": "~/shared.db",
        "prefetch_k": 3,
        "auto_sync": False,
    }


def test_config_schema_covers_every_saved_key(provider):
    assert {f["key"] for f in provider.get_config_schema()} == {
        "db_path",
        "prefetch_k",
        "auto_sync",
    }


# ---------------------------------------------------------------------------
# 3. backup_paths — declared only when the store sits outside HERMES_HOME
# ---------------------------------------------------------------------------


def test_backup_paths_empty_for_profile_scoped_store(provider):
    assert provider.backup_paths() == []


def test_backup_paths_declares_a_shared_store(backend, tmp_path, monkeypatch):
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    shared = tmp_path.parent / "shared" / "memware.db"
    (tmp_path / "memware.json").write_text(json.dumps({"db_path": str(shared)}))

    # Must resolve from config alone — no initialize(), no network.
    assert MemwareMemoryProvider().backup_paths() == [str(shared)]


# ---------------------------------------------------------------------------
# 4. Prefetch — current beliefs only, bounded
# ---------------------------------------------------------------------------


def test_prefetch_formats_current_beliefs(provider, backend):
    backend["hits"] = [_Hit("api", "port", "8443")]
    block = provider.prefetch("which api port")

    assert "- api port: 8443 (since 2026-01-02)" in block


@pytest.mark.parametrize("query", ["", "   "])
def test_prefetch_skips_empty_queries(provider, query):
    assert provider.prefetch(query) == ""


def test_prefetch_disabled_at_zero(provider, backend):
    backend["hits"] = [_Hit("api", "port", "8443")]
    provider._prefetch_k = 0
    assert provider.prefetch("which api port") == ""


def test_prefetch_never_raises(provider, monkeypatch):
    import memware.index as index_mod

    def _boom(*a, **kw):
        raise RuntimeError("index corrupt")

    monkeypatch.setattr(index_mod, "search_beliefs", _boom)
    assert provider.prefetch("anything") == ""  # a broken store must not break the turn


# ---------------------------------------------------------------------------
# 5. Capture — non-blocking, profile-scoped, idempotent
# ---------------------------------------------------------------------------


def test_sync_turn_is_threaded_and_writes_the_session_file(provider, tmp_path, backend):
    provider.sync_turn("what does deploy do", "it runs blue-green rollouts", session_id="s9")
    assert provider._sync_thread is not None and provider._sync_thread.daemon
    provider.shutdown()  # joins

    path = tmp_path / "memware" / "sessions" / "s9.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert all(r["session"] == "s9" for r in rows)
    assert backend["synced"] == [(str(path), "generic")]


def test_sync_turn_stores_text_only_never_raw_messages(provider, tmp_path):
    provider.sync_turn(
        [{"type": "text", "text": "hello"}],
        [{"type": "text", "text": "hi"}, {"type": "tool_use", "input": {"secret": "x"}}],
        session_id="s1",
        messages=[{"role": "tool", "content": "SECRET-TOOL-OUTPUT"}],
    )
    provider.shutdown()

    written = (tmp_path / "memware" / "sessions" / "s1.jsonl").read_text()
    assert "hello" in written and "SECRET-TOOL-OUTPUT" not in written


def test_auto_sync_off_writes_nothing(provider, tmp_path):
    provider._auto_sync = False
    provider.sync_turn("q", "a", session_id="s2")
    provider.shutdown()

    assert not (tmp_path / "memware" / "sessions" / "s2.jsonl").exists()


def test_session_end_and_pre_compress_flush(provider, tmp_path, backend):
    provider.sync_turn("q", "a", session_id="sess-1")
    provider.shutdown()
    backend["synced"].clear()

    provider.on_session_end([])
    assert provider.on_pre_compress([]) == ""

    path = str(tmp_path / "memware" / "sessions" / "sess-1.jsonl")
    assert backend["synced"] == [(path, "generic"), (path, "generic")]


def test_session_switch_flushes_then_rebinds(provider, tmp_path, backend):
    provider.sync_turn("q", "a", session_id="sess-1")
    provider.shutdown()
    backend["synced"].clear()

    provider.on_session_switch("sess-2", parent_session_id="sess-1", reset=True)

    assert backend["synced"] == [
        (str(tmp_path / "memware" / "sessions" / "sess-1.jsonl"), "generic")
    ]
    assert provider._session_id == "sess-2"

    provider.sync_turn("q2", "a2")
    provider.shutdown()
    assert (tmp_path / "memware" / "sessions" / "sess-2.jsonl").exists()


# ---------------------------------------------------------------------------
# 6. Built-in memory mirroring
# ---------------------------------------------------------------------------


def test_memory_write_mirrors_as_a_human_stated_belief(provider, backend):
    provider.on_memory_write("add", "preferences", "always use pnpm not npm")

    assert backend["beliefs"] == [
        {
            "subject": "preferences",
            "relation": "note",
            "value": "always use pnpm not npm",
            "reliability": 0.9,
            "source": "hermes built-in memory (add)",
        }
    ]


@pytest.mark.parametrize(("action", "content"), [("remove", "x"), ("replace", "x"), ("add", "   ")])
def test_memory_write_ignores_non_additive_and_empty(provider, backend, action, content):
    provider.on_memory_write(action, "memory", content)
    assert backend["beliefs"] == []


# ---------------------------------------------------------------------------
# 7. Tools
# ---------------------------------------------------------------------------


def test_tool_schemas_are_declared(provider):
    assert {t["name"] for t in provider.get_tool_schemas()} == {
        "memware_recall",
        "memware_read_session",
        "memware_remember",
        "memware_beliefs",
    }


def test_recall_merges_beliefs_and_turns(provider, backend):
    backend["hits"] = [_Hit("api", "port", "8443")]
    out = json.loads(provider.handle_tool_call("memware_recall", {"query": "api port"}))

    assert out[0]["kind"] == "belief" and out[0]["subject"] == "api"


def test_remember_reports_the_ledger_outcome(provider, backend):
    out = json.loads(
        provider.handle_tool_call(
            "memware_remember", {"subject": "api", "relation": "port", "value": "8443"}
        )
    )

    assert out == {"outcome": "committed", "belief_id": 7, "review_id": None}
    assert backend["beliefs"][0]["reliability"] == 0.7


def test_read_session_and_beliefs_route(provider):
    assert json.loads(provider.handle_tool_call("memware_read_session", {"session": "s9"})) == [
        {"session": "s9"}
    ]
    assert json.loads(provider.handle_tool_call("memware_beliefs", {"subject": "api"})) == [
        {"subject": "api"}
    ]


def test_tool_errors_are_returned_not_raised(provider):
    assert json.loads(provider.handle_tool_call("nope", {}))["error"].startswith("unknown tool")
    # a missing required argument is reported to the model, not raised at the agent
    assert "error" in json.loads(provider.handle_tool_call("memware_recall", {}))


def test_register_registers_the_provider():
    from plugins.memory.memware import register

    registered = []
    register(types.SimpleNamespace(register_memory_provider=registered.append))

    assert len(registered) == 1 and registered[0].name == "memware"
