# memware for Claude Code

Hooks: `SessionEnd` and `PreCompact` sync the transcript; `UserPromptSubmit`
injects the few beliefs relevant to the prompt, each with the date it was recorded, less the
stale ones (`memware beliefs --stale`), and nothing on a background task's notification or
inside a subagent. Remove the
`UserPromptSubmit` entry if you prefer purely on-demand recall.

Skill: `/memware:garden` measures whether injected beliefs are relevant (with the optional
relevance filter in `shadow` or `filter` mode), proposes one setting change per cycle, and tends
the ledger: beliefs that keep arriving as noise, and stale, orphaned and duplicate ones. It
changes nothing without your yes. Run it about weekly.

Requires the `memware` CLI on `PATH` (`pip install "memware[mcp]"`). Set
`MEMWARE_DB` to relocate the database.
