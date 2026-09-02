# Integrations

## Claude Code

`integrations/claude-code/` is a Claude Code plugin. Install from a local
checkout while it is unpublished:

```bash
claude plugin marketplace add ./integrations/claude-code   # or your fork's path
claude plugin install memware
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

Tools: `recall`, `read_session`, `beliefs`, `remember`, `pending_reviews`.

Subagents: the plugin does not inject into subagents. They can call the MCP
tools. Their transcripts are synced with the parent session's.

## Hermes Agent

`integrations/hermes/` is a memory-provider plugin skeleton for
[Hermes Agent](https://github.com/NousResearch/hermes-agent). It shells out to
the `memware` CLI: `prefetch` → `memware context`, `on_session_end` →
`memware sync --harness generic` on an exported session. It is experimental
and tracks Hermes's provider interface loosely; read its README.

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
