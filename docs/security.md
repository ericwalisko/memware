# Security model

memware keeps your coding-agent transcripts in one SQLite file and injects facts derived from them
into later prompts. This page covers what it holds, who and what can reach it, what the code does
about each path, and what it does not do. To report a vulnerability, see
[SECURITY.md](../SECURITY.md).

## What memware holds

| Asset | Where | Why it matters |
|---|---|---|
| Turn text | the store (`memware.db`, its `-wal` and `-shm`) | Everything typed or pasted into a session: secrets, private code, other people's documents. |
| Beliefs | the store | Short facts that are injected into later prompts without anyone asking. |
| Snapshots | `backup.dest` | Full copies of the store. |
| Transcript mirror | `backup.dest/transcripts` | Copies of the harness's own transcripts. |
| Config and state | the memware home (`~/.memware` or `$XDG_DATA_HOME/memware`) | Paths, the relevance log (prompts), a `.env` that may hold a provider key. |

## Who and what can reach it

1. **You**, the owner. Trusted. memware can't protect anything from your own user account.
2. **Other accounts on the same machine.** Untrusted. They reach memware only through the
   filesystem.
3. **Content that enters a session**, such as a pasted README, a web page, an issue, or a tool
   result. Untrusted. It is indexed, derive can file facts from it, and those facts are injected
   later.
4. **The agent**, meaning the model behind Claude Code or Hermes. Semi-trusted. It calls the MCP
   tools (`recall`, `read_session`, `beliefs`, `remember`, `pending_reviews`). Whatever it last
   read can steer it.
5. **Model providers.** `memware derive` sends excerpts to the provider you chose. The relevance
   filter is off by default and sends prompts and candidate facts to its endpoint when you turn it
   on. Both are documented, opt-in data flows.
6. **Backup destinations**, such as a synced folder or a network disk. They are as private as the
   service behind them.

Out of scope:

- An attacker running as your user.
- A tampered memware package.
- Recovering deleted data from the disk. [SECURITY.md](../SECURITY.md) covers what `prune` can and
  can't reach.

## Findings and hardening

Severity is rated for a default install with the MCP server registered and `derive` in use.

| ID | Surface | Severity | Status |
|---|---|---|---|
| MW-SEC-1 | File modes of the store, snapshots and the memware home | High on a shared machine, Low on a single-user one | Hardened |
| MW-SEC-2 | Text reaching FTS5 `MATCH` | Low | No injection found; hardened against slow queries |
| MW-SEC-3 | MCP tool arguments | Medium | Hardened |
| MW-SEC-4 | Beliefs injected into later prompts | High | Hardened; residual risk below |
| MW-SEC-5 | Redaction matching in `memware prune` | Medium | Hardened; residual risk below |

### MW-SEC-1: File modes

memware used to set no file modes, so what it wrote took the process umask. Now:

- **Store.** Created empty at 0600 before SQLite opens it. SQLite gives the `-wal` and `-shm`
  files the database's mode.
- **Directories memware creates.** 0700, including missing parents: the memware home, a new
  store directory, a new backup destination, and the transcript mirror's directories.
- **Snapshots.** Written into a file pre-created at 0600. `VACUUM INTO` fills an empty file and
  keeps its mode.
- **Restored store and its pre-restore copy.** 0600.
- **Transcript mirror copies, `config.json` and the review outbox.** 0600.

**Migration for existing stores.** Every open tightens the store file, its `-wal`, `-shm` and
`-journal` to 0600. When the store is in the memware home, the home goes to 0700. Only files your
user owns are changed. An existing directory named by `MEMWARE_DB` or `backup.dest` is left alone,
because you may share it on purpose. A mode that can't be set never stops a store from opening.

The code is in `memware.fsperm`, and `tests/test_security_permissions.py` holds the tests.

### MW-SEC-2: FTS5 queries

All user and agent text goes through `memware.index.fts_query` before it reaches `MATCH`. That
covers recall, the prompt hook, the digest's project match and the subject rule.
`fts_query` keeps only keywords made of word characters and `-./`, and it quotes each one. So no
FTS5 syntax survives: no double quote, column filter, `NEAR`, `*`, `^` or boolean operator. A
fuzz test runs hostile and random queries through every caller and checks that the query shape
holds and nothing raises.

A long dotted or slashed run, such as a minified line, is still one keyword. FTS5 reads a quoted
keyword as a phrase of its pieces, so a runaway keyword made a very slow search. Keywords longer
than `MAX_TERM_CHARS` (128) are now left out. A path or a version is well under that. There are
still at most 24 keywords per query.

The tests are in `tests/test_security_fts.py`.

### MW-SEC-3: MCP tool arguments

No tool takes a filesystem path. A session id is only an SQL parameter, and the store is
`MEMWARE_DB`, fixed when the server starts. So `read_session` has no path traversal. Every SQL
statement is parameterized; the f-strings in `src/` insert only table and column names from the
code.

Arguments are now bounded before they reach the store:

- `recall` reads at most 16 phrasings, and `k` is kept within 1–50.
- `read_session` keeps `window` within 0–50. An `around` outside SQLite's integer range reads
  nothing instead of raising.
- `remember` accepts `valid_from` only as an ISO-8601 time no more than a day ahead, and stores it
  as UTC. The ledger orders a key's values by this field, so it must be a real time.

`read_session` without `around` still returns the whole session. The client limits how much it
reads.

The tests are in `tests/test_security_mcp.py`.

### MW-SEC-4: Injected beliefs as standing instructions

A belief is injected into later prompts without anyone asking for it, through the prompt hook, the
session-start digest and the Hermes prefetch. Content that entered a session once can phrase an
order to the agent. If that text became a belief, it would come back in every matching session as
an instruction. Neither extraction nor injection involves a model at decision time, so the
mitigation is deterministic and has two layers:

1. **When the belief is filed.** `derive.validate` refuses a triple that is instruction-shaped
   (`memware.instruction.instruction_shaped`). That covers:
   - a demand to drop earlier instructions
   - an order addressed to the agent
   - a standing order ("from now on")
   - an action hidden from the user
   - a download piped into a shell
   - deleting a home or root directory
   - a role or prompt marker
   - a value or relation that opens with an order

   The match runs after NFKC normalization with invisible characters removed, so fullwidth letters
   and zero-width spaces don't hide it.
2. **When the belief would be injected.** The injection gate (`memware.volatile.Gate`) leaves out
   every instruction-shaped belief. This applies whoever wrote it and whatever its reliability or
   source, and it covers stores written before this change. Such a belief stays in the ledger, in
   `memware beliefs`, in recall and in the MCP `beliefs` tool, marked `volatile: "instruction"`.
   `memware beliefs --stale` and `--explain` list it. `memware stats` counts it. The Hermes
   prefetch refuses the mark through `Gate.admits_hit`.

Every line the prompt hook and the digest inject is also rendered as one line of plain text
(`memware.instruction.one_line`). Control and line-separator characters become spaces, and
invisible and bidirectional-control characters are removed. So a value can't start a line or a
turn of its own, or hide words from someone reading `memware beliefs`. Text as derive writes it
comes back byte-identical.

The tests are in `tests/test_security_injection.py`. They include:
- an attack corpus
- a check that no belief in the test fixtures is flagged
- derive refusing a grounded instruction
- the gate leaving out a human-stated instruction
- the prompt hook end to end

### MW-SEC-5: Redaction

`memware prune --apply` redacts a secret from every belief that holds it and refuses a redaction
too broad to be a secret's. A copy it missed would stay in the ledger and keep being injected,
while the prune reported the value gone. Belief matching (`memware.ledger.secret_pattern`) is
still case-sensitive, because a secret is. It now also sees the forms one secret takes in text:

- composed or decomposed Unicode (NFC or NFD)
- fullwidth forms
- percent-encoding in a URL or connection string, with hex digits in either case
- an invisible character inside the secret (zero-width space, soft hyphen, bidirectional mark)

The refusal measures what will actually match, meaning the visible characters, composed. Invisible
padding or decomposed letters can no longer make a short text pass the six-character minimum. A
text with nothing visible matches nothing.

`tests/test_security_redaction.py` holds the tests, including a seeded fuzz test. It disguises
random secrets in random fields and checks that none survive and that no other belief changes.

## Reviewed, no change needed

- **Derive's extraction call.** `claude -p` runs with `--tools ""`, `--max-turns 1`, no session
  persistence, and a neutral working directory. A transcript excerpt can't make the extraction
  model run a tool or load a project's instructions.
- **Transcript parsing.** Transcripts are parsed as JSON only, with no `eval` and no pickle.
- **Network access.** Only the derive provider you choose and the opt-in relevance filter.

## Residual risks

- **A false fact isn't an order.** The injection filter recognizes the shapes of an instruction,
  not the truth of a fact. Planted text phrased as a plain fact passes it. An example is a wrong
  URL given as a project's installer. Injected beliefs are recalled notes, each with the date it
  was recorded. To reduce the risk:
  - Read `memware beliefs` now and then, and retract what is wrong with
    `memware beliefs retract`.
  - Leave `derive` off, or exclude the paths (`memware exclude`), for sessions that ingest
    untrusted text.
- **Agent-written beliefs count as a person's.** The `remember` tool and the Hermes
  `memware_remember` tool write with the reliability and source the agent passes. Such a belief is
  exempt from the volatility classes, though never from the instruction filter.
  - Recommended follow-up: stamp agent writes with their own provenance, so that they're gated
    like derived beliefs until a person confirms them from a terminal. It changes the ledger
    contract, so it needs a design note in [design.md](design.md).
- **Filter false positives.** A durable rule phrased as an order, such as "never push to main", is
  not injected unasked. It is still in recall, and `memware beliefs --explain` says why.
- **Redaction limits.** These are still not matched:
  - a copy in another letter case
  - a base64 or other encoded copy
  - a secret split across fields
  - turn text: the prune's turn selector and `memware scan` stay literal

  Rotate a credential that leaked.
- **Hermes.** The Hermes provider writes its session transcripts under
  `<hermes home>/memware/sessions` with the default umask. It builds its injected lines itself, so
  they aren't flattened, though the gate's instruction filter still applies. Its tools don't apply
  the MCP bounds.
- **Shared locations are left as they are.** An existing directory named by `MEMWARE_DB` or
  `backup.dest` is not tightened, and a sync service's own sharing settings apply to snapshots.
- **Windows.** Modes are POSIX only. On Windows the store inherits the directory's ACL.
- **Data at rest is unencrypted.** Full-disk encryption is the control. See
  [SECURITY.md](../SECURITY.md).
- **The agent can read what memware holds.** `recall` and `read_session` return transcript text
  to the agent that asks. The MCP server isn't a confidentiality boundary against the agent it
  serves, and that agent can usually read the transcripts directly anyway.
