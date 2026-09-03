# Integrations

## Claude Code

`integrations/claude-code/` is a Claude Code plugin and the repository is its
marketplace:

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
