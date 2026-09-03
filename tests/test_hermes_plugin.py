"""Contract tests for the Hermes provider, run without Hermes installed (stub ABC)."""

import importlib.util
import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "integrations" / "hermes" / "memware" / "__init__.py"


def load(tmp_path):
    spec = importlib.util.spec_from_file_location("memware_hermes_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = mod.MemwareProvider()
    (tmp_path / "hermes").mkdir()
    (tmp_path / "hermes" / "memware.json").write_text(
        json.dumps({"db_path": str(tmp_path / "m.db")})
    )
    p.initialize("sess-1", hermes_home=str(tmp_path / "hermes"))
    return mod, p


def test_register_and_identity(tmp_path):
    mod, p = load(tmp_path)

    class Ctx:
        got = None

        def register_memory_provider(self, prov):
            Ctx.got = prov

    mod.register(Ctx())
    assert Ctx.got.name == "memware" and p.is_available()
    assert {t["name"] for t in p.get_tool_schemas()} == {
        "memware_recall",
        "memware_read_session",
        "memware_remember",
        "memware_beliefs",
    }


def test_prefetch_returns_only_current_beliefs(tmp_path):
    _, p = load(tmp_path)
    p.handle_tool_call(
        "memware_remember",
        {"subject": "api", "relation": "port", "value": "8080", "reliability": 0.5},
    )
    p.handle_tool_call(
        "memware_remember",
        {"subject": "api", "relation": "port", "value": "8443", "reliability": 0.5},
    )
    block = p.prefetch("which api port")
    assert "8443" in block and "8080" not in block
    assert p.prefetch("") == ""


def test_sync_turn_is_non_blocking_and_indexes(tmp_path):
    _, p = load(tmp_path)
    p.sync_turn(
        "what does the deploy script do",
        "the deploy script runs blue-green rollouts",
        session_id="s9",
    )
    p.shutdown()  # joins the daemon thread
    out = json.loads(
        p.handle_tool_call("memware_recall", {"query": "blue-green rollout", "what": "turns"})
    )
    assert out and out[0]["session"] == "s9"
    f = tmp_path / "hermes" / "memware" / "sessions" / "s9.jsonl"
    assert f.exists() and len(f.read_text().splitlines()) == 2
    p.on_session_end([], session_id="s9")  # idempotent re-sync
    again = json.loads(
        p.handle_tool_call("memware_recall", {"query": "blue-green rollout", "what": "turns"})
    )
    assert len(again) == len(out)


def test_memory_write_mirrors_as_belief_and_config_roundtrip(tmp_path):
    _, p = load(tmp_path)
    p.on_memory_write("add", "preferences", "always use pnpm not npm")
    beliefs = json.loads(p.handle_tool_call("memware_beliefs", {"subject": "preferences"}))
    assert beliefs and "pnpm" in beliefs[0]["value"] and beliefs[0]["reliability"] == 0.9
    p.save_config(
        {"db_path": "~/x.db", "prefetch_k": "3", "auto_sync": "false"}, str(tmp_path / "hermes")
    )
    cfg = json.loads((tmp_path / "hermes" / "memware.json").read_text())
    assert cfg == {"db_path": "~/x.db", "prefetch_k": 3, "auto_sync": False}
    assert json.loads(p.handle_tool_call("nope", {}))["error"].startswith("unknown tool")
