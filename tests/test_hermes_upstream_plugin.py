"""End-to-end tests for the upstream-packaged provider, against the real store.

``integrations/hermes/upstream/`` is shaped for hermes-agent's tree, so its own
tests (``tests/plugins/memory/test_memware_provider.py``) only run inside a
hermes-agent checkout and stub the memware package. Nothing there exercises the
real store — that is this file's job. It loads the same source by path behind
stand-ins for the three hermes modules it imports, then drives it against a
genuine SQLite store.

The last test is a drift guard: two copies of a provider live in this repo and
they must agree on the surface a user sees.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "integrations" / "hermes" / "upstream" / "plugins" / "memory" / "memware"
IN_TREE = ROOT / "integrations" / "hermes" / "memware" / "__init__.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def hermes_stubs(monkeypatch, tmp_path):
    """Stand in for the hermes modules the upstream copy imports."""
    memory_provider = types.ModuleType("agent.memory_provider")
    memory_provider.MemoryProvider = type("MemoryProvider", (), {})
    agent = types.ModuleType("agent")
    agent.memory_provider = memory_provider

    lazy_deps = types.ModuleType("tools.lazy_deps")
    lazy_deps.ensure = lambda feature, **kw: None
    tools = types.ModuleType("tools")
    tools.lazy_deps = lazy_deps

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path

    for name, module in {
        "agent": agent,
        "agent.memory_provider": memory_provider,
        "tools": tools,
        "tools.lazy_deps": lazy_deps,
        "hermes_constants": constants,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture()
def provider(hermes_stubs, tmp_path):
    module = _load(UPSTREAM / "__init__.py", "memware_upstream_plugin")
    prov = module.MemwareMemoryProvider()
    prov.initialize("sess-1", hermes_home=str(tmp_path))
    return prov


def test_store_is_created_under_the_profile(provider, tmp_path):
    assert Path(provider._db) == tmp_path / "memware" / "memware.db"
    assert Path(provider._db).exists()  # schema created eagerly at initialize()


def test_prefetch_injects_only_the_latest_value(provider):
    for port in ("8080", "8443"):
        provider.handle_tool_call(
            "memware_remember",
            {"subject": "api", "relation": "port", "value": port, "reliability": 0.5},
        )

    block = provider.prefetch("which api port")

    assert "8443" in block and "8080" not in block


def test_a_turn_is_captured_and_recallable(provider, tmp_path):
    provider.sync_turn(
        "what does the deploy script do",
        "the deploy script runs blue-green rollouts",
        session_id="s9",
    )
    provider.shutdown()  # joins the daemon thread

    hits = json.loads(
        provider.handle_tool_call(
            "memware_recall", {"query": "blue-green rollout", "what": "turns"}
        )
    )
    assert hits and hits[0]["session"] == "s9"

    path = tmp_path / "memware" / "sessions" / "s9.jsonl"
    assert len(path.read_text().splitlines()) == 2

    provider.on_session_end([])  # re-indexing the same file must not duplicate turns
    again = json.loads(
        provider.handle_tool_call(
            "memware_recall", {"query": "blue-green rollout", "what": "turns"}
        )
    )
    assert len(again) == len(hits)


def test_a_session_switch_keeps_each_session_separate(provider, tmp_path):
    """Turns must land in the session that was live when they happened."""
    provider.sync_turn("how is the scheduler wired", "the scheduler polls a work queue")
    provider.shutdown()
    provider.on_session_switch("sess-2")
    provider.sync_turn("how is the scheduler wired", "the scheduler polls a work queue")
    provider.shutdown()

    hits = json.loads(
        provider.handle_tool_call(
            "memware_recall", {"query": "scheduler work queue", "what": "turns"}
        )
    )
    assert {h["session"] for h in hits} == {"sess-1", "sess-2"}


def test_builtin_memory_writes_land_as_beliefs(provider):
    provider.on_memory_write("add", "preferences", "always use pnpm not npm")

    beliefs = json.loads(provider.handle_tool_call("memware_beliefs", {"subject": "preferences"}))
    assert beliefs and "pnpm" in beliefs[0]["value"] and beliefs[0]["reliability"] == 0.9


def test_config_round_trips_and_reloads(hermes_stubs, provider, tmp_path):
    provider.save_config(
        {"db_path": "$HERMES_HOME/other.db", "prefetch_k": "3", "auto_sync": "false"}, str(tmp_path)
    )
    assert json.loads((tmp_path / "memware.json").read_text()) == {
        "db_path": "$HERMES_HOME/other.db",
        "prefetch_k": 3,
        "auto_sync": False,
    }

    reloaded = _load(UPSTREAM / "__init__.py", "memware_upstream_reload").MemwareMemoryProvider()
    reloaded.initialize("sess-2", hermes_home=str(tmp_path))
    assert Path(reloaded._db) == tmp_path / "other.db"
    assert reloaded._prefetch_k == 3 and reloaded._auto_sync is False


def test_the_two_copies_expose_the_same_surface(provider, hermes_stubs, tmp_path):
    """Both copies ship in this repo; a user must see the same provider."""
    in_tree = _load(IN_TREE, "memware_hermes_plugin_intree").MemwareProvider()

    assert in_tree.name == provider.name
    assert {t["name"] for t in in_tree.get_tool_schemas()} == {
        t["name"] for t in provider.get_tool_schemas()
    }
    assert {f["key"] for f in in_tree.get_config_schema()} == {
        f["key"] for f in provider.get_config_schema()
    }


def test_declared_hooks_are_implemented():
    import yaml

    declared = yaml.safe_load((UPSTREAM / "plugin.yaml").read_text())
    source = (UPSTREAM / "__init__.py").read_text()

    assert declared["name"] == "memware"
    assert declared["pip_dependencies"] == ["memware"]
    for hook in declared["hooks"]:
        assert f"def {hook}(" in source, f"plugin.yaml declares {hook} but it is not implemented"
