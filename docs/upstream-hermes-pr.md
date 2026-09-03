# Draft PR: add a `memware` memory provider

Draft text for a pull request to
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
contributing the provider staged in `integrations/hermes/upstream/`.

**Not submitted.** No fork exists and no PR has been opened. This file is the
text to review first; see the Status section of
`integrations/hermes/upstream/README.md` for what is still open.

---

## Title

`Add memware memory provider (local bi-temporal belief ledger + transcript index)`

## Body

### What this adds

A new memory provider plugin, `plugins/memory/memware/`, backed by
[memware](https://github.com/ericwalisko/memware) (MIT): a local SQLite store
holding a bi-temporal **belief ledger** alongside an FTS5 **transcript index**.

The problem it targets is stale memory rather than missing memory. Most stores
accumulate: assert that a service listens on port 8080, later assert 8443, and
both are retrievable — so a prompt can carry a value that stopped being true
months ago, and the agent has no way to tell which one is current. memware
records the change as a supersession: the old row is closed with an end time
rather than deleted, and reads filter to rows still valid. Recall returns 8443.
`memware history` still shows 8080 and when it stopped holding.

### Why it might be worth having in-tree

The eight bundled providers cover cloud services (Honcho, Mem0, Hindsight,
RetainDB, Supermemory) and two local stores (Holographic, ByteRover). This adds
a local option with a different retrieval model:

- **Temporal correctness over recall volume.** Prefetch injects a handful of
  currently valid facts, never a superseded value. Transcript search is on
  demand through a tool, so an ordinary turn costs one indexed query — no LLM
  extraction call, no network round-trip on the critical path.
- **No account, no server, no credentials.** One file. `is_available()` is
  unconditionally true, so it works on a fresh install and in air-gapped or
  offline use.
- **Changes can be gated rather than applied silently.** A contested
  supersession — a new value conflicting with a well-supported existing one —
  can be queued for human review instead of overwriting.
- **One store, several clients.** The store is a single file, so pointing
  several tools at one path lets them share a memory. memware also ships a CLI
  and an MCP server.

### How it works

| Hook | Behaviour |
|---|---|
| `system_prompt_block` | Tells the model its injected facts are current, and to record changes when it learns of one |
| `prefetch` | Top-`k` currently valid beliefs relevant to the turn (default 6, `0` disables) |
| `sync_turn` | Appends the turn to `$HERMES_HOME/memware/sessions/<id>.jsonl` and indexes it from a byte-offset cursor, on a daemon thread |
| `on_session_end`, `on_pre_compress`, `on_session_switch` | Flush and re-index; idempotent, and a switch lands in-flight writes in the outgoing session first |
| `on_memory_write` | Mirrors built-in `MEMORY.md` adds into the ledger as human-stated beliefs |
| `backup_paths` | Declares the store when the user has moved it outside `HERMES_HOME` |

Tools: `memware_recall`, `memware_read_session`, `memware_remember`,
`memware_beliefs`.

Points where the guide's contracts shaped the implementation:

- **`is_available()` does not import `memware`.** Gating availability on the
  import is the chicken-and-egg described in
  `test_memory_lazy_install.py`: the provider never loads, so `initialize()`
  never runs, so `ensure()` never installs the package. There is nothing else
  to gate on — the provider is local and takes no credentials.
- **A single lazy-install chokepoint.** `_ensure_memware()` calls
  `ensure("memory.memware", prompt=False)`, and every method that imports
  `memware.*` passes through it.
- **Profile isolation.** The store defaults to
  `$HERMES_HOME/memware/memware.db`, with `$HERMES_HOME` and `~` expanded in
  user-supplied paths, following the holographic provider.
- **`sync_turn()` never blocks a turn**, and joins any previous sync thread
  before starting the next.
- **Turn capture stores text only.** The optional `messages` argument is
  accepted and deliberately ignored: it can carry tool arguments and command
  output. Nothing is sent off-device either way.

### Changes

New:

- `plugins/memory/memware/{__init__.py,plugin.yaml,config_schema.py,README.md}`
- `tests/plugins/memory/test_memware_provider.py`

Edited:

- `tools/lazy_deps.py` — one `LAZY_DEPS` entry, `"memory.memware"`, pinned
  exactly to match the file's no-ranges policy
- `website/docs/user-guide/features/memory-providers.md` — provider section,
  comparison-table row, provider count, and the profile-isolation note

No changes to core files. Discovery is by directory scan, so there is no
registry to register in.

### Testing

`tests/plugins/memory/test_memware_provider.py` — 30 tests, no new dependency.
The `memware` package is stubbed the way `test_memory_lazy_install.py` stubs
the supermemory and mem0 SDKs, and the pip subprocess is never run. Covered:

- `is_available()` stays true with the package unimportable, and
  `memory.memware` resolves in the `LAZY_DEPS` allowlist — the two contracts
  whose absence caused the earlier silent-dark failures
- config resolution, `$HERMES_HOME` expansion, type coercion, and a
  round-trip that preserves unrelated keys already in the file
- `backup_paths()` empty for a profile-scoped store, populated for a shared
  one, and resolvable without `initialize()` or network
- capture: threaded and daemonised, correct session file, `auto_sync` off,
  idempotent re-index, session-switch ordering, and that raw `messages` never
  reach disk
- prefetch formatting, the empty-query and `k=0` short-circuits, and that a
  broken store returns `""` instead of breaking the turn
- tool routing, and that tool errors are returned to the model rather than
  raised at the agent

Behaviour against a real store is covered in memware's own repository, which
runs the same provider source against a genuine SQLite store on Python 3.11,
3.12, and 3.13.

### Docs

An entry in `website/docs/user-guide/features/memory-providers.md` in the house
format — capability table, tools, architecture, setup, config reference — plus a
`README.md` in the plugin directory.

### Checklist

- [ ] `memware` published on PyPI, and the `LAZY_DEPS` pin set to that version
- [ ] `pytest tests/plugins/memory/test_memware_provider.py` green
- [ ] `hermes memory setup` lists memware from a clean checkout
- [ ] Provider activates, prefetches, and captures turns end to end on a clean
      install
- [ ] Repository CI green
