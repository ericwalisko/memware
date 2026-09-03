# Integrations

## Claude Code

The plugin's hooks call `memware` as a bare command, so install the CLI on your PATH
**as a tool** first (a plain `pip install` into a project/conda env usually leaves it off
the hook shell's PATH, and the hooks then silently do nothing):

```bash
uv tool install "memware[mcp]"    # or: pipx install "memware[mcp]"
memware --version                  # must resolve
```

Then add the plugin (the repository is its own marketplace):

```bash
claude plugin marketplace add ericwalisko/memware
claude plugin install memware@memware
```

Hooks (`hooks/hooks.json`):

| event | command | effect |
|---|---|---|
| `SessionEnd`, `PreCompact` | `memware sync --harness claude-code --from-hook` | indexes the session's new turns from `transcript_path` |
| `UserPromptSubmit` (optional) | `memware context --from-hook` | injects the few currently valid beliefs relevant to the prompt as `additionalContext` |

The prompt-time hook injects **beliefs only**, capped by `-k`. Transcript
search is on demand through the MCP server:

```bash
claude mcp add memware -- memware-mcp
```

On a new machine, index existing transcripts once with `memware backfill` (defaults to
`~/.claude/projects`, idempotent); the plugin only captures sessions from then on.

Tools: `recall` (takes a list of phrasings — have the agent pass 3–5, including synonyms and the literal value it expects), `read_session`, `beliefs`, `remember`, `pending_reviews`.

Subagents: the plugin does not inject into subagents. They can call the MCP
tools. Their transcripts are synced with the parent session's.

## Hermes Agent

`integrations/hermes/memware/` is a memory-provider plugin implementing
[Hermes Agent](https://github.com/NousResearch/hermes-agent)'s `MemoryProvider`
ABC: prompt-time belief `prefetch`, non-blocking `sync_turn` capture, session
flush, built-in-memory mirroring, and four tools for iterative recall. Install by
copying it to `$HERMES_HOME/plugins/memware/` and running `hermes memory setup`.
Both plugins share one store by default, so Claude Code and Hermes remember the
same things.

`integrations/hermes/upstream/` stages the same provider packaged as
hermes-agent's own `plugins/memory/<name>/` tree, for contributing it in-tree so
`hermes memory setup` lists memware on a clean install with no manual copy. It
is prepared, not submitted — see that directory's README for what is still open
and `docs/upstream-hermes-pr.md` for the draft PR text.

## Any other harness

Export sessions as message JSONL (`role`, `content`, `timestamp`, optional
`session`) and run `memware sync <dir> --harness generic`. Add a parser under
`memware/ingest/` for a native format — it is one generator function.

## Review channel

Contested supersessions are published through a `ReviewBackend`:

- `memware review sync` — JSONL outbox/inbox under `~/.memware/`
- `memware review sync --url https://your.host/memware --token …` — HTTP:
  `POST /reviews`, `GET /decisions`

Implement either side in whatever tool you use to make decisions.
