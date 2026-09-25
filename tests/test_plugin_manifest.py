"""The Claude Code plugin carries its own version string, and `claude plugin update` only
reinstalls when it rises. Nothing in the release path enforced it, so it silently sat at 0.1.1
across many releases and no hook change ever reached an installed machine. These tests keep both
manifests equal to the package version, so cutting a release always moves the plugin too.

The Hermes plugin has the same shape of problem. Hermes' package manager installs the plugin's
`python_dependencies` into its venv, but it keeps whatever version its lock already holds while the
requirement is still satisfied, so only a raised floor moves Hermes onto a new release."""

import json
from pathlib import Path

import memware

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "claude-code" / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
HERMES_PLUGIN = ROOT / "integrations" / "hermes" / "memware" / "plugin.yaml"


def test_plugin_manifest_version_matches_package():
    plugin = json.loads(PLUGIN.read_text())
    assert plugin["version"] == memware.__version__, (plugin["version"], memware.__version__)


def test_marketplace_plugin_version_matches_package():
    market = json.loads(MARKETPLACE.read_text())
    entry = next(p for p in market["plugins"] if p["name"] == "memware")
    assert entry["version"] == memware.__version__, (entry["version"], memware.__version__)


def _python_dependencies(manifest: str) -> list[str]:
    # Read without a YAML parser: the release job runs this suite with pytest alone.
    lines = manifest.splitlines()
    items = []
    for line in lines[lines.index("python_dependencies:") + 1 :]:
        if not line.startswith("  - "):
            break
        items.append(line.removeprefix("  - ").strip().strip("\"'"))
    return items


def test_hermes_plugin_requires_this_release():
    deps = _python_dependencies(HERMES_PLUGIN.read_text())
    assert deps == [f"memware>={memware.__version__}"], (deps, memware.__version__)
