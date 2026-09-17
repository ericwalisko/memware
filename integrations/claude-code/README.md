# memware for Claude Code

Hooks: `SessionEnd` and `PreCompact` sync the transcript; `UserPromptSubmit`
injects the few beliefs relevant to the prompt, each with the date it was recorded, less the
stale ones (`memware beliefs --stale`). Remove the
`UserPromptSubmit` entry if you prefer purely on-demand recall.

Requires the `memware` CLI on `PATH` (`pip install "memware[mcp]"`). Set
`MEMWARE_DB` to relocate the database.
