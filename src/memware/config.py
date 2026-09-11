"""User configuration at ``$MEMWARE_HOME/config.json`` (default ``~/.memware``).

Small and explicit: the store path, and a backup block. Everything has a safe default,
so memware works with no config file at all; ``memware setup`` writes one interactively.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def memware_home() -> Path:
    """Directory for config, markers, and (by default) the store.

    Resolution, first match wins — so an existing install is never migrated and a new one
    follows the XDG Base Directory spec:

    1. ``$MEMWARE_HOME`` — explicit override.
    2. ``~/.memware`` — if it already exists (a pre-existing install stays put).
    3. ``$XDG_DATA_HOME/memware`` — a fresh install when XDG is configured.
    4. ``~/.memware`` — the default.
    """
    env = os.environ.get("MEMWARE_HOME")
    if env:
        return Path(env).expanduser()
    legacy = Path("~/.memware").expanduser()
    if legacy.exists():
        return legacy
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "memware"
    return legacy


def config_path() -> Path:
    return memware_home() / "config.json"


DEFAULTS: dict[str, Any] = {
    # Where daily snapshots go. A plain filesystem path, so it is storage-agnostic:
    # a Dropbox/iCloud/Drive folder, an external disk, or a network mount all work.
    "backup": {
        "dest": None,  # e.g. "~/Dropbox/memware" or "/Volumes/backup/memware"
        "keep_days": [1, 3, 7, 14],  # tiered retention: newest snapshot per age bucket
        "include_transcripts": True,  # also mirror the transcript source into <dest>/transcripts
        "transcript_src": "~/.claude/projects",  # what to mirror / where backfill also reads
        "auto": True,  # when a dest is set, the session-end hook backs up ~once/day (no cron needed)
        "auto_interval_hours": 20,  # minimum gap between automatic backups
    },
    # `memware derive`: fill the ledger from the transcripts (docs/scheduling.md).
    "derive": {
        "auto": False,  # let the plugin's session-start hook run it (~once/day, no cron needed)
        "provider": "claude-code",  # claude-code (the Claude Code CLI, no key) | openai
        "model": None,  # provider default (claude-code: haiku)
    },
}


def load_user_config() -> dict[str, Any]:
    """The config file as written, with no DEFAULTS merged in. Missing file or bad JSON reads as
    empty. Write back through this, not ``load_config()``: saving the merged view would record
    every default as if the user had chosen it."""
    try:
        user = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return user if isinstance(user, dict) else {}


def load_config() -> dict[str, Any]:
    """Config merged over DEFAULTS. Missing file or bad JSON falls back to defaults."""
    cfg: dict[str, Any] = json.loads(json.dumps(DEFAULTS))  # deep copy
    for k, v in load_user_config().items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def save_config(cfg: dict[str, Any]) -> Path:
    home = memware_home()
    home.mkdir(parents=True, exist_ok=True)
    p = config_path()
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return p


def get_dotted(cfg: dict[str, Any], key: str) -> Any:
    cur: Any = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def has_key(key: str) -> bool:
    """Whether the config file itself sets ``key``. ``load_config()`` cannot say: a default of
    False reads the same as a user who answered False."""
    parent, _, leaf = key.rpartition(".")
    user = load_user_config()
    node = get_dotted(user, parent) if parent else user
    return isinstance(node, dict) and leaf in node


def set_dotted(cfg: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
