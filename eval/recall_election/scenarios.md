# Recall election scenarios

Each scenario in `scenarios.json` is a prompt a developer might type in Claude Code while sitting
inside `fixture_project/`. Its label is the right first move: `recall` when the memware recall
tool is needed to answer at all, `no_recall` when the working tree answers the prompt fully and a
recall call would be noise. The cell's only built-in tools are Grep, Read and Glob, so every
answer a negative needs has to be reachable by reading.

## Rubric

- **Positive (`recall`).** The prompt asks about a value or construct that is in the tree: a
  port, a timeout, a pin, a marker comment, a skipped test. No file says why, or what the marker
  refers to. The prompt has no history cue: none of "decided", "last time", "earlier", "we",
  "chat", "session" or "before", and it also avoids "originally", "chosen", "picked" and "our".
  To elect recall, the model has to see that a why-question about present code has no answer in
  the code.
- **Negative (`no_recall`).** Grep, Read and Glob answer the prompt completely. Since the
  toolset is read-only, edit, refactor and test prompts ask where a change would go, what it
  would touch, or what a test asserts. They never ask for an edit or a test run.
- **Bait negative (`history_bait`).** The prompt uses history words ("why", "legacy",
  "originally", "decided"), but a comment, docstring or README line in the tree states the
  reason. A description that fires on the words alone elects here; one that sends the model to
  the tree first does not.
- Prompts never mention recall, memory or notes.
- A scenario where either election is defensible gets rewritten. Every negative below cites
  the line that answers it. For every positive, a grep over the tree found no stated reason.

## Class balance

24 scenarios, 12 positives and 12 negatives.

| label | class | n | ids |
|---|---|---|---|
| recall | `rationale` | 12 | `rationale_1` to `rationale_12` |
| no_recall | `find_code` | 1 | `find_code_1` |
| no_recall | `edit_task` | 2 | `edit_task_1`, `edit_task_2` |
| no_recall | `explain_file` | 2 | `explain_file_1`, `explain_file_2` |
| no_recall | `refactor` | 2 | `refactor_1`, `refactor_2` |
| no_recall | `run_tests` | 1 | `run_tests_1` |
| no_recall | `style_question` | 1 | `style_question_1` |
| no_recall | `history_bait` | 3 | `history_bait_1` to `history_bait_3` |

All positives share one class, so the per-class table in `report.md` shows only the overall TPR
for them. The per-scenario table has the detail, and the construct each one targets is listed
below.

## What changed from the 2026-09-10 set, and why

The 2026-09-10 run (8 variants x 2 models) flagged 13 of 24 scenarios as having no signal:
every copy answered them the same way. Ten of the twelve positives had explicit history cues
("did we decide", "last time", "in chat", "we looked at", "the runner repo"), so every variant
elected recall on them at 1.00. Only `rationale_1` and `rationale_2` had no cue, and they were
the only positives where variants differed (baseline on sonnet missed both). Among the
negatives, no variant ever elected on `find_code_2`, `run_tests_2` or `style_question_2`.

- **Kept verbatim (6):** `rationale_1`, `rationale_2`, `find_code_1`, `explain_file_1`,
  `explain_file_2`, `style_question_1`.
- **Reworded, id and class kept (5):** `edit_task_1`, `edit_task_2`, `refactor_1`,
  `refactor_2`, `run_tests_1`. Each asked for an edit or a test run that Grep/Read/Glob cannot
  do, which blurred what was being measured. `run_tests_1` ("Run the test suite...") elected
  recall in 9 of 16 cells, often with queries about how to run the tests: that measured the
  missing Bash tool, not the description. Each one is now a read-only question that the tree
  answers.
- **Removed (13):** `past_decision_1`, `past_decision_2`, `rejected_alternative_1`,
  `rejected_alternative_2`, `earlier_session_1`, `earlier_session_2`, `cross_repo_1`,
  `cross_repo_2`, `not_in_tree_1`, `not_in_tree_2`, `find_code_2`, `run_tests_2`,
  `style_question_2`.
- **Added (13):** `rationale_3` to `rationale_12` (ten cue-free positives modelled on
  `rationale_1`/`rationale_2`, each on a different construct) and `history_bait_1` to
  `history_bait_3` (three negatives with history bait that the tree answers).

Results from 2026-09-10 are not comparable with runs on this set: both the scenarios and the
fixture changed.

## Positives: construct and grep check

Each construct was grepped with context, plus a sweep for reason words (`because|so that|
reason|why|avoid|in order|purpose|originally|legacy|workaround|temporary|deliberat|decid|chosen|
picked|instead|rather|since|due to|otherwise`). The sweep hit five lines. Three are the bait
lines listed under the negatives, and none of them explains anything a positive asks about.
The other two are the bare markers that `rationale_2` and `rationale_7` ask about.

| id | construct | where it sits | no reason in the tree |
|---|---|---|---|
| `rationale_1` | listen port 8443 | `config.py:17,30`, `.env.example:3`, `README.md:12` | the README gives only the default |
| `rationale_2` | `# legacy:` marker on `sign` | `auth.py:59` | nothing names a successor (`bearer`, `jwt`, `ed25519`, `mtls`, `replace`: no hits) |
| `rationale_3` | upstream timeout 4.0 s | `config.py:19,32`, `.env.example:5` | value only |
| `rationale_4` | linear retry backoff | `server.py:52`, `README.md:24-25` | README says "linear", not why |
| `rationale_5` | stdlib only, no framework | `README.md:5`, `pyproject.toml:6` | stated as a fact, no reason |
| `rationale_6` | `requires-python = ">=3.11"` | `pyproject.toml:5` | nothing in the code needs 3.11; the newest feature used (`X \| None` in unpostponed test annotations) needs 3.10 |
| `rationale_7` | `# workaround:` on the forwarded Content-Type | `server.py:38-39` | the comment says where it applies, not what it works around |
| `rationale_8` | `MAX_BODY_BYTES = 262_144` | `config.py:11`, `server.py:100`, `README.md:20-21` | value and 413 only |
| `rationale_9` | `ruff>=0.5,<0.6` | `pyproject.toml:9` | pin only |
| `rationale_10` | bare `@pytest.mark.skip` | `tests/test_server.py:39-43` | no skip reason; the test passes when unskipped |
| `rationale_11` | `Handler.log_message` is a no-op | `server.py:59-60` | no comment or docstring |
| `rationale_12` | `KEY_ID_PATTERN = k[0-9]{1,3}` | `auth.py:19,45-46` | pattern and error message only |

## Negatives: the line that answers each

| id | answer in the tree |
|---|---|
| `find_code_1` | `config.py:17` `port: int = 8443`, `config.py:30` reads `GATEWAY_PORT`, `.env.example:3`; bound at `server.py:131` |
| `edit_task_1` | `Handler.do_GET`, next to the `/healthz` branch at `server.py:85-87` and before the `/v1/jobs/` 404 at `:88`; `__version__` from `src/gateway/__init__.py:3`, already imported at `server.py:13` |
| `edit_task_2` | `config.py:18` (field), `config.py:31` (`GATEWAY_UPSTREAM_URL`), `server.py:33`, `server.py:138`, `.env.example:4` |
| `explain_file_1` | `auth.py`: module docstring `:1-6`, header constants and key-id pattern `:16-19`, `KeyRing` `:26-40`, `rotate_keys` `:43-52`, `canonical` `:55-56`, `sign` `:59-61`, `verify` `:64-88` |
| `explain_file_2` | `auth.py:43-52` stamps the old id in `retired_at` and adds the new key; `auth.py:35-40` `usable` keeps it valid until `grace_seconds` pass; `README.md:32-35` |
| `refactor_1` | six call sites: `server.py:80, 89, 96, 101-102, 106, 113`; each takes a status and a message |
| `refactor_2` | `config.py:30` int (port), `config.py:32` float (upstream_timeout), `config.py:35` int (max_skew_seconds) |
| `run_tests_1` | `tests/test_server.py:61-72`: `urlopen` patched to raise `URLError` (`:64-68`), `time.sleep` patched to a no-op (`:69`), asserts `UpstreamError` (`:70`) and `len(calls) == RETRY_LIMIT` (`:72`) |
| `style_question_1` | module-level uppercase names: `auth.py:16-19`, `config.py:8-11`; no class-attribute constants anywhere |
| `history_bait_1` | `README.md:17-19`: "Unauthenticated on purpose: liveness probes carry no signing key, and the route reveals nothing beyond liveness and the package version." |
| `history_bait_2` | `pyproject.toml:13-14`: "Legacy name, kept because the deployed systemd units still start the gateway as gateway-server; dropping it would break every host that has not had its unit file updated." |
| `history_bait_3` | `server.py:126-128`: "The gateway originally ran on plain HTTPServer, which handles one request at a time, so a single slow runner call stalled every client queued behind it." |

## Fixture changes for this set

- `config.py`: `MAX_BODY_BYTES = 262_144` (for `rationale_8`).
- `server.py`: a 413 check on the body length in `do_POST` (`rationale_8`); a `# workaround:`
  line above the forwarded Content-Type (`rationale_7`); a no-op `Handler.log_message`
  (`rationale_11`); a `serve()` docstring giving the ThreadingHTTPServer reason
  (`history_bait_3`).
- `auth.py`: `KEY_ID_PATTERN` and a check in `rotate_keys` (`rationale_12`).
- `pyproject.toml`: ruff pinned `<0.6` (`rationale_9`); a `gateway-server` script with a comment
  explaining it (`history_bait_2`).
- `README.md`: the reason `/healthz` is unauthenticated (`history_bait_1`); the body cap; the
  retry sentence corrected to say "attempted up to RETRY_LIMIT times", which is what `forward`
  does.
- `tests/test_server.py`: `test_unknown_key_id_is_rejected` with a bare skip (`rationale_10`).

The fixture still passes its suite (6 passed, 1 skipped), `ruff check` and `ruff format --check`.
Rules for any later edit: never add a reason for anything a positive asks about. Do not add
`CLAUDE.md`, `.claude/` or `.git`. Do not use the words eval, fixture, recall, stub or harness
inside `fixture_project/`, because the model reads those files. Do not leave tool caches
(`.ruff_cache`, `__pycache__`, `.pytest_cache`) in the tree; run its tests on a copy.

## Borderline calls

- `rationale_1` (kept as instructed): 8443 is the usual unprivileged HTTPS port, so a model can
  give a general-knowledge reason that is not the project's.
- `rationale_5`: generic reasons for staying stdlib-only (supply chain, footprint) are easy to
  offer without checking.
- `rationale_9`: a model may list what ruff 0.6 changed. Which change matters here is not in
  the tree.
- `rationale_11`: "it silences BaseHTTPRequestHandler's stderr log" says what the override does,
  not why. The prompt asks for the reason behind running without request logs to rule that
  answer out.
- `history_bait_1` has the heaviest bait ("decided on purpose, or is it a gap?"). The README
  line answers both halves.
