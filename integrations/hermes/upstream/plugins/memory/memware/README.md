# memware Memory Provider

Local memory in one SQLite file: a **belief ledger** whose reads only return the
value that currently holds, and a **transcript index** over past sessions.

When a fact changes, the previous value is closed out rather than deleted — the
prompt never carries a superseded value, and the history stays auditable. No
account, no network, no credentials.

## Requirements

- `pip install memware` (installed on first use)

## Setup

```bash
hermes memory setup      # select "memware", accept the defaults
```

Or manually:

```bash
hermes config set memory.provider memware
```

## Config

`$HERMES_HOME/memware.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/memware/memware.db` | SQLite file. `$HERMES_HOME` and `~` are expanded |
| `prefetch_k` | `6` | Currently valid beliefs injected before each turn. `0` disables injection |
| `auto_sync` | `true` | Index every completed turn for later recall |

Storage is profile-scoped by default. Setting `db_path` to `~/.memware/memware.db`
shares one store with other memware clients (the `memware` CLI, the MCP server,
the Claude Code plugin) so they remember the same things; `backup_paths()` then
declares it to `hermes backup`.

## Tools

| Tool | Description |
|------|-------------|
| `memware_recall` | Ranked, dated snippets from past sessions and current beliefs |
| `memware_read_session` | Read a session in order, or a window around one turn |
| `memware_remember` | Record `(subject, relation, value)`; supersedes the old value |
| `memware_beliefs` | List what is currently true, optionally for one subject |

## Behaviour

| Hook | What it does |
|------|--------------|
| `prefetch` | Injects the few currently valid beliefs relevant to the turn — never a superseded value |
| `sync_turn` | Appends the turn to `$HERMES_HOME/memware/sessions/<id>.jsonl` and indexes it on a daemon thread |
| `on_session_end`, `on_pre_compress`, `on_session_switch` | Flush and re-index; idempotent |
| `on_memory_write` | Mirrors built-in `MEMORY.md` adds into the ledger as human-stated beliefs |

Prefetch injects **beliefs only** — bounded and always current. Transcript search
is on demand through `memware_recall`, so an ordinary turn costs one FTS query
and no LLM call.

Only the user and assistant text of a turn is stored; the raw `messages` list,
which can carry tool arguments and command output, is not. Nothing leaves the
device either way.

## Upstream

Source and issues: <https://github.com/ericwalisko/memware>
