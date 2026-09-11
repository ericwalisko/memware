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
| `SessionStart` | `memware sync` (catch-up) + `memware backup --if-stale 20`, then `memware derive --apply --auto --if-stale 24` (a no-op until `memware config derive.auto true`), all backgrounded | indexes any session whose `SessionEnd` never ran, then a throttled backup — see note |
| `SessionStart` | `memware notice --from-hook`, in the foreground | until `memware setup` has asked about `derive` (setup last ran before 0.4.0 or never, and `derive.auto` is unset), shows one line under the session header saying so; reads only the config, and prints nothing once either is true, after a compaction, or when the config will not parse |
| `SessionStart` | `memware digest --from-hook`, in the foreground (5 s timeout) | injects a block of at most 1,200 characters: a line pointing at `recall`, this project's 5 most recent sessions (date and first prompt), and the currently valid beliefs whose subject names the project; nothing for a project memware has no session for — see [the digest](#the-session-start-digest) |
| `SessionEnd`, `PreCompact` | `memware sync --harness claude-code --from-hook` | indexes the session's new turns from `transcript_path` |
| `UserPromptSubmit` (optional) | `memware context --from-hook` | injects the few currently valid beliefs relevant to the prompt as `additionalContext` |

`SessionEnd` runs when Claude Code exits cleanly, but some environments **force-kill** it (a worktree/pane manager may `SIGKILL` the process group on close), and a `SIGKILL` cannot run any hook. The `SessionStart` hook covers that: it runs a bare `memware sync` — which catches up the configured `backup.transcript_src` (default `~/.claude/projects`) — plus a throttled backup, **backgrounded** so it never delays startup. So the previous session is indexed at the next start even if its `SessionEnd` was skipped; the raw transcript is durable on disk regardless.

That catch-up and the backup run without the environment of the session they pick up, so a session started with `MEMWARE_NO_CAPTURE=1` is kept out another way. Every `--from-hook` entry that runs under the variable (`notice`, `digest`, `context`, `sync`) adds the payload's `transcript_path` to `<home>/no-capture.txt`, and every sync and backup skips what is listed. In payloads captured from Claude Code 2.1.268, `SessionStart`, `UserPromptSubmit` and `SessionEnd` all carry `transcript_path`, even with `--no-session-persistence` (the file is then never written); `PreCompact` did not fire in that probe. The start entries record the path before the first prompt. Recording prints nothing and never changes a hook's output. See [keeping-memory-clean.md](keeping-memory-clean.md#what-the-switch-cannot-do) for what it cannot catch.

A backgrounded hook's output reaches nobody, so the notice is a separate foreground entry. Claude Code shows its `systemMessage` to you, not to the model: someone who only uses the plugin never runs `memware stats` and would otherwise never learn that `derive` exists. It takes a few tens of milliseconds, and it exits 0 with no output if `memware` is missing from the hook's `PATH` or is too old to know `notice`.

The prompt-time hook injects **beliefs only**, capped by `-k`. Transcript
search is on demand through the MCP server. Add it at **user** scope so every
project sees it — the default (`local`) scopes the server to the one directory
you run the command in, and sessions in other projects won't find the tools:

```bash
claude mcp add -s user memware -- memware-mcp
claude mcp list        # confirm it's registered; tools load at the next session start
```

On a new machine, index existing transcripts once with `memware backfill` (defaults to
`~/.claude/projects`, idempotent); the plugin only captures sessions from then on.

Tools: `recall` (takes a list of phrasings — have the agent pass 3–5, including synonyms and the literal value it expects), `read_session`, `beliefs`, `remember`, `pending_reviews`.

Subagents: the plugin does not inject into subagents. They can call the MCP
tools. Their transcripts are synced with the parent session's.

### The session-start digest

The prompt hook injects nothing until the ledger has beliefs, so for most sessions the digest is
the first memware content the model sees. That matters because the model only recalls when it
chooses to. Run `memware digest` in a project directory to see the block the hook injects. Like
the notice, the hook exits 0 with no output if `memware` is missing from the hook's `PATH` or is
too old to know `digest`.

The digest is scoped by transcript path, with no search. Claude Code keeps a project's
transcripts in `~/.claude/projects/<the directory, every non-alphanumeric character a dash>/`
(under `$CLAUDE_CONFIG_DIR` when that is set), so only sessions from that directory count.
Inside a git repository the project is the whole repository: the primary checkout and every
live linked worktree, read from git's own files. The block lists the most recent sessions
(`-k`, default 5) and the currently valid beliefs whose subject shares a whole word with the
directory name, the repository name, or the package name in `pyproject.toml` or
`package.json`, up to `--max-chars` (default 1,200). The session that is starting is left out.
Nothing the digest reads counts as a recall.

Two limits:

- **Claude Code transcripts only.** Other harnesses have no per-project transcript layout, so
  sessions indexed from Hermes or with `--harness generic` never appear in the digest. They
  remain searchable through `recall`.
- **Removed worktrees drop out.** Git stops listing a worktree once it is removed, so sessions
  held only in a torn-down worktree leave the digest. `recall` still finds them.

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
