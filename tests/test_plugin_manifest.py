"""The Claude Code plugin carries its own version string, and `claude plugin update` only
reinstalls when it rises. Nothing in the release path enforced it, so it silently sat at 0.1.1
across many releases and no hook change ever reached an installed machine. These tests keep both
manifests equal to the package version, so cutting a release always moves the plugin too."""

import json
from pathlib import Path

import memware

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "claude-code" / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"


def test_plugin_manifest_version_matches_package():
    plugin = json.loads(PLUGIN.read_text())
    assert plugin["version"] == memware.__version__, (plugin["version"], memware.__version__)


def test_marketplace_plugin_version_matches_package():
    market = json.loads(MARKETPLACE.read_text())
    entry = next(p for p in market["plugins"] if p["name"] == "memware")
    assert entry["version"] == memware.__version__, (entry["version"], memware.__version__)
