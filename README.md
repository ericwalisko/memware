# memware

**Memory for AI agents that only remembers the latest truth.**

memware is one SQLite file with two stores:

- **turns** — immutable evidence. Every prompt and answer from past sessions,
  split into ~400-token passages and indexed with FTS5. Recall ranks passages and
  quotes only the matching ones; reading a session back returns whole turns.
  BM25 × recency × use, no model in the loop.
- **beliefs** — a bi-temporal ledger of facts. A new value for the same
  `(subject, relation)` **supersedes** the old one. Recall only ever returns the
  currently valid belief; history is kept for audit and never reaches a prompt.

No daemon, no vector database, no LLM call at capture or read time. A 30-day
corpus of a busy coding agent — 18k turns, 40k passages — indexes in about
fourteen seconds into ~120 MB.

```text
$ memware sync ~/.claude/projects --harness claude-code
{"added": 14348, "files": 1475}

$ memware assert "api" "listens on port" "8443" --source "session 3f2a, turn 41"
{"outcome": "superseded", "belief_id": 2, "incumbent_id": 1}

$ memware recall "which port does the api use" --what beliefs
api listens on port 8443            # 8080 is in the ledger, retired, and never surfaces
```

## Why

Agent memory systems that rewrite what they remember degrade: continuous
LLM consolidation can push utility *below* having no memory at all
([Useful Memories Become Faulty When Continuously Updated by LLMs](https://arxiv.org/abs/2605.12978)).
And embeddings cannot tell a *contradicted* fact from a *rephrased* one — AUROC 0.59 —
so vector stores serve stale facts 15–40% of the time on evolving knowledge
([Temporal Validity in Retrieval Memory](https://arxiv.org/abs/2606.26511)).

memware borrows four mechanisms from human memory research and keeps them deliberately small:

| mechanism | in the brain | in memware |
|---|---|---|
| evidence ≠ belief | hippocampus vs neocortex (complementary learning systems) | `turn` table is append-only; `belief` table is separate |
| update on surprise | reconsolidation driven by prediction error | `memware assert` at the moment an agent notices a conflict |
| only the latest understanding | reconsolidated traces overwrite in place | deterministic supersession keyed on `(subject, relation)`, ordered by **event time** |
| need-probability recall | Anderson & Schooler 1991 / ACT-R activation | `bm25 × (1+age)^-d × (1 + w·ln(1+uses))` |

Full rationale and citations: [docs/design.md](docs/design.md).

## Install

The Claude Code plugin's hooks call `memware` as a bare command, so the CLI must be on
the `PATH` your shell uses — **install it as a tool, not into a project virtualenv**:

```bash
uv tool install "memware[mcp]"     # recommended
# or
pipx install "memware[mcp]"
```

Then confirm the shim resolves (if this prints nothing, the hooks will silently do nothing):

```bash
memware --version
which memware
```

<details>
<summary>Plain <code>pip install</code></summary>

`pip install "memware[mcp]"` works for library and CLI use, but a plain pip install into a
project or conda environment usually leaves `memware` off the PATH that Claude Code's hooks
run under — use `uv tool` or `pipx` (above) for the plugin, or install into an environment
that is always active. `memware` (core) omits the MCP server; drop `[mcp]` only if you do
not want the MCP tools.

</details>

## Use it from Claude Code

```bash
claude plugin marketplace add ericwalisko/memware
claude plugin install memware@memware
claude mcp add -s user memware -- memware-mcp   # optional tools; -s user = every project, not just this dir
```

**Backfill your existing sessions** (optional, once). The plugin only captures new
sessions; index the transcripts already on disk so recall works over past work from day one:

```bash
memware backfill                 # indexes ~/.claude/projects (idempotent; ~5 s for a month)
```

Prefer a guided first run? `memware setup` walks through the backfill and backups together and
prints the operating guidance — safe on a fresh install and after upgrading from a pre-0.2
(no-backups) version; `memware setup --yes` accepts the defaults non-interactively.

The *belief ledger* starts empty and is not backfilled — beliefs are derived, not stored in
transcripts. It fills as you work (via the `remember` tool, or a derive job you schedule).
Transcript recall is what backfill gives you immediately, and it is where most of the value is.

Requires the `memware` CLI on your `PATH` (see [Install](#install)). Hooks:
a `SessionStart` hook catches up any session whose `SessionEnd` was skipped (some environments force-kill Claude Code — a worktree manager may `SIGKILL` it — and a kill cannot run `SessionEnd`); `SessionEnd`/`PreCompact` sync the transcript into the index; an optional `UserPromptSubmit`
hook injects the handful of currently valid beliefs whose subject the prompt names (beliefs
only — transcript search is on demand through the MCP tools). Set `MEMWARE_DB` to move the
store, and `MEMWARE_NO_CAPTURE=1` for any session you do not want indexed. See
[docs/integrations.md](docs/integrations.md) and [docs/keeping-memory-clean.md](docs/keeping-memory-clean.md).

## Use it from Hermes Agent

`integrations/hermes/memware/` is a memory-provider plugin built on Hermes's
`MemoryProvider` ABC — prompt-time belief prefetch, non-blocking turn capture, and
`memware_recall` / `memware_remember` tools — sharing one store with Claude Code.

## The supersession rule

```text
same key, same value   → reinforce (reliability rises, use is counted)
same key, newer value  → supersede: incumbent gets valid_to = new.valid_from
same key, older value  → filed as history; the timeline stays consistent
weaker challenger      → parked as a candidate and sent to review
```

Ordering is decided by `valid_from` (when the evidence says it became true), never by
insertion order — so a backfill converges to the same state in any order, twice, or in
batches. Three policies: `auto` (last writer by event time), `gate_conflicts`
(default: a less reliable challenger goes to review), `await_confirmation`.

## Recall is keyword search; the agent supplies the meaning

The index is FTS5/BM25 — fast, model-free, and literal. The `recall` tool therefore takes
**several phrasings** and fuses them by reciprocal rank, so a tool-calling agent puts its
own reasoning into retrieval at call time (synonyms, related concepts, the literal value it
expects), the same way it would issue a few grep or web-search queries:

```text
recall(queries=["which port does the api listen on", "api port", "8443", "gateway listen port"])
```

Byte-identical hits collapse to a single slot, so a prompt captured on many days — a scheduled job's own preamble, say — never crowds out distinct evidence; the turns stay in the store and a session still reads back whole.

Prompt-time injection (the hooks) stays deterministic and only injects beliefs whose
*subject* the prompt names.

## Backups and the wipe trap

Transcripts are deleted by the OS after ~30 days, so an aged session lives only in the store —
**back it up, and never wipe-and-re-backfill** (backfill only re-indexes transcripts still on
disk). memware guards this: migrations snapshot first, and `backfill` warns if a backup is
larger than the store. Once a destination is set, backups happen **automatically at session boundaries** — the
`SessionStart` hook takes a throttled snapshot (at most once every ~20h), and a clean
`SessionEnd` does too. No cron; immune to a laptop sleeping through a scheduled time, and — because
the start hook always runs — also to a session being force-killed (a worktree/pane manager that
`SIGKILL`s Claude Code never runs `SessionEnd`).

```bash
memware setup                              # guided: index sessions, pick a folder, take a first backup
memware backup                             # tiered snapshot (1/3/7/14-day) + transcript mirror
memware restore --latest                   # after a wipe, restore — do not re-backfill
memware nuke                               # delete everything, typed-confirmation guarded
```

Full guide: [docs/backup.md](docs/backup.md).

## Keeping evaluations out of the evidence

Full guide: [docs/keeping-memory-clean.md](docs/keeping-memory-clean.md).

Headless runs write transcripts too. Set `MEMWARE_NO_CAPTURE=1` in any run you do not want
indexed (hooks, the Hermes provider and `memware sync --from-hook` all honour it), put
`[memware-eval]` in evaluation prompts, and use `memware-eval --corpus ROOT --db scratch.db
--beliefs-from ~/.memware/memware.db` to judge retrieval against a store that excludes them.
`memware prune --containing TEXT` un-indexes runs that already slipped in. For a durable filter that every sync honours — including runs that predate a marker — list content signatures in `~/.memware/ignore-markers.txt` (or `MEMWARE_IGNORE_MARKERS`); any transcript whose head contains one is never indexed.

## Reviewing contested supersessions

memware does not ship a UI. It ships a contract — `ReviewBackend` with `publish()` and
`collect()` — plus two implementations: JSONL outbox/inbox files and a plain HTTP
endpoint. Wire it to whatever you already use to make decisions.

```bash
memware review sync                       # outbox ~/.memware/review-outbox.jsonl
echo '{"review_id": 7, "decision": "approve"}' >> ~/.memware/review-inbox.jsonl
memware review sync                       # applied
```

## Evaluation

`memware-eval` scores retrieval against a question set: does the right evidence
surface, and does the stale value stay hidden? It needs no model, so results are
reproducible. The protocol for end-to-end comparisons — agent alone vs agent + memware —
is in [docs/eval.md](docs/eval.md).

## Editor and shell integration

memware ships no editor plugins — `--plain` (tab-separated, id-first) and `--json` are the
integration surface, and everything is a copy-paste recipe on top of them. Shell completions
come from `memware completions zsh|bash|fish` (needs the `[shell]` extra:
`uv tool install "memware[mcp,shell]"`). `--plain` pipes cleanly to `fzf`/`awk`/`cut`:

```bash
memware recall "which port does the api use" --plain | fzf --delimiter='\t' --with-nth=10
```

Emacs, Vim, Neovim, an `$EDITOR` bulk-edit round-trip, and completion install steps are in
[docs/editor-integration.md](docs/editor-integration.md).

## Accessibility

memware emits no colour at all (so `NO_COLOR` is honoured by construction), and no information
is ever carried by colour. Default output is screen-reader-friendly — labeled, one field per
line, blank line between records; `--plain` and `--json` are the stable machine formats; and
`--ascii` (auto-on in a non-UTF-8 locale) avoids glyphs a screen reader or terminal might
mangle. Full statement: [docs/accessibility.md](docs/accessibility.md).

## Status

Alpha. The schema may change before 1.0; the ledger semantics will not.

## License

MIT. See [LICENSE](LICENSE).
