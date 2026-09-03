<!--
Draft entry for hermes-agent's website/docs/user-guide/features/memory-providers.md.

This is a fragment, not a replacement: the file upstream documents every
provider. Four edits, all in that one file.

1. Frontmatter `description` — append `, memware` to the provider list.
2. Intro line — "ships with 8 external memory provider plugins" becomes 9.
3. The `memory: provider:` example comment — append `, memware`.
4. Insert the `### memware` section below after `### Supermemory` and before
   `### Memori` (Memori installs from PyPI rather than shipping in-tree, and
   stays last), then add the Provider Comparison row and the Profile
   Isolation mention that follow it here.
-->

### memware

Local belief ledger plus transcript index in one SQLite file. Reads return only the value that currently holds: when a fact changes, the previous value is closed out rather than deleted, so the prompt never carries a superseded value while the history stays auditable.

| | |
|---|---|
| **Best for** | Long-lived local memory where facts change and stale values are the problem |
| **Requires** | `pip install memware` (installed on first use). Nothing else — no account, no server |
| **Data storage** | Local SQLite |
| **Cost** | Free |

**Tools (4):** `memware_recall` (ranked, dated snippets from past sessions and current beliefs), `memware_read_session` (a session in order, or a window around one turn), `memware_remember` (record `subject`/`relation`/`value`; a new value supersedes the old one), `memware_beliefs` (what is currently true)

**Architecture:** Two stores, one file. The **belief ledger** is bi-temporal — every fact carries the event time it became true and the time it was recorded, and superseding a fact closes the old row instead of overwriting it. Queries filter to currently valid rows, so a superseded value cannot reach a prompt, and `memware history` still shows the whole timeline. The **transcript index** is FTS5 over past turns, ranked by relevance decayed by age and lifted by how often a hit has proved useful.

Prefetch injects **beliefs only** — a handful of short, current facts. Transcript search is on demand through `memware_recall`, so an ordinary turn costs one indexed query and no LLM call.

**Setup:**
```bash
hermes memory setup    # select "memware"
# Or manually:
hermes config set memory.provider memware
```

**Config:** `$HERMES_HOME/memware.json`

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/memware/memware.db` | SQLite file. `$HERMES_HOME` and `~` are expanded |
| `prefetch_k` | `6` | Currently valid beliefs injected before each turn. `0` disables injection |
| `auto_sync` | `true` | Index every completed turn for later recall |

**One store, several clients:** the store is a single file, so pointing `db_path` at a shared location (for example `~/.memware/memware.db`) makes Hermes, the `memware` CLI, and the MCP server remember the same things. It is profile-scoped by default; a shared path is declared to `hermes backup` through `backup_paths()`.

**Privacy:** everything is local and nothing is sent off-device. Only the user and assistant text of a turn is stored — not the raw `messages` list, which can carry tool arguments and command output.

**Unique capabilities:**
- Supersession as a first-class operation — the model can correct a known fact mid-conversation and every later prompt reflects it
- Contested changes can be gated for human review instead of applied silently (`memware review`)
- Retrieval ranking that decays with age and rises with proven usefulness

---

<!-- Provider Comparison — add this row after **Supermemory**: -->

| **memware** | Local | Free | 4 | `memware` | Bi-temporal ledger — recall only ever returns the current value |

<!-- Profile Isolation — amend the local-storage bullet to read: -->

- **Local storage providers** (Holographic, ByteRover, memware) use `$HERMES_HOME/` paths which differ per profile
