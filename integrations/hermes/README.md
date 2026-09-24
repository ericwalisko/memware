# memware for Hermes Agent

A memory-provider plugin implementing Hermes's `MemoryProvider` ABC.

```bash
cp -R integrations/hermes/memware "$HERMES_HOME/plugins/memware"   # default HERMES_HOME=~/.hermes
hermes memory setup      # select "memware"; accept the defaults; Hermes installs memware for you
```

`plugin.yaml` declares `python_dependencies: ["memware>=X.Y.Z"]`. Hermes' package manager
installs memware from PyPI into Hermes' own venv during `hermes memory setup`, and puts it back
in every venv it rebuilds later (`hermes update`, `hermes pm install`, a desktop-app update).
Restart the gateway afterwards. Do not `pip install` memware into that venv by hand: Hermes does
not record a hand install, and its next rebuild drops it. If `hermes memory status` reports memware
`not available`, run `hermes pm install`, not `hermes pm repair`: repair rebuilds the package set
Hermes already recorded and does not add a new declaration. A Hermes old enough to have no
`hermes pm` needs `pip install memware` in the Python env it runs.

Hermes keeps the memware version already in its lock as long as the floor is met. To move
Hermes to a new release, copy the plugin from that release (its floor names the release), run
`hermes pm install`, and restart the gateway.

| hook / tool | what it does |
|---|---|
| `prefetch` | injects the few current beliefs relevant to the turn, each with the date it was recorded (never a superseded value, and never a derived measurement, moving version or status; see [what injection leaves out](../../docs/integrations.md#what-injection-leaves-out)); with the optional, off-by-default [relevance filter](../../README.md#optional-a-relevance-filter-for-prompt-time-injection) on, only those it judges relevant |
| `sync_turn` | appends the completed turn to `<hermes_home>/memware/sessions/<id>.jsonl` and indexes it, in a daemon thread |
| `on_session_end` / `on_pre_compress` | flushes and re-syncs the session file |
| `on_memory_write` | mirrors built-in `MEMORY.md` adds into the ledger as human-stated beliefs |
| `memware_recall`, `memware_read_session` | iterative recall over past sessions (ranked, dated snippets) |
| `memware_remember`, `memware_beliefs` | supersede a fact; list what is currently true |

Config (`hermes memory setup` → `<hermes_home>/memware.json`): `db_path` (default
`~/.memware/memware.db`, shared with Claude Code and the CLI), `prefetch_k`, `auto_sync`.
The relevance filter is not a Hermes setting. `prefetch` reads `relevance.mode` from memware's own
config (`memware config relevance.mode …`), and reads `TYPESAFE_API_KEY` from Hermes's
environment or `~/.memware/.env`, so one switch covers both harnesses. The switch is off by
default. When it is on, each turn's query and its candidate beliefs are sent to TypeSafe.

Set `MEMWARE_NO_CAPTURE=1` in Hermes's environment and the provider captures nothing: no session
file, no indexed turn, no mirrored memory write. The tools still work.

Only one external provider can be active at a time in Hermes.
