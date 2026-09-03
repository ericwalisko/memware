# memware for Hermes Agent

A memory-provider plugin implementing Hermes's `MemoryProvider` ABC.

```bash
pip install memware                                   # in the Python env Hermes uses
cp -R integrations/hermes/memware "$HERMES_HOME/plugins/memware"   # default HERMES_HOME=~/.hermes
hermes memory setup                                   # select "memware"; accept the defaults
```

| hook / tool | what it does |
|---|---|
| `prefetch` | injects the few currently valid beliefs relevant to the turn (never a superseded value) |
| `sync_turn` | appends the completed turn to `<hermes_home>/memware/sessions/<id>.jsonl` and indexes it, in a daemon thread |
| `on_session_end` / `on_pre_compress` | flushes and re-syncs the session file |
| `on_memory_write` | mirrors built-in `MEMORY.md` adds into the ledger as human-stated beliefs |
| `memware_recall`, `memware_read_session` | iterative recall over past sessions (ranked, dated snippets) |
| `memware_remember`, `memware_beliefs` | supersede a fact; list what is currently true |

Config (`hermes memory setup` → `<hermes_home>/memware.json`): `db_path` (default
`~/.memware/memware.db`, shared with Claude Code and the CLI), `prefetch_k`, `auto_sync`.

Only one external provider can be active at a time in Hermes.
