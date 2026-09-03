# Upstream staging: `plugins/memory/memware` for hermes-agent

This directory holds the memware memory provider **packaged the way
[hermes-agent](https://github.com/NousResearch/hermes-agent) expects it**, so
contributing it in-tree is a copy rather than a rewrite. Once it ships upstream,
`hermes memory setup` lists memware on a clean install and no manual copy into
`$HERMES_HOME/plugins/` is needed.

Nothing here loads from this repo: the provider imports `agent.memory_provider`,
`hermes_constants`, and `tools.lazy_deps`, which only exist inside a
hermes-agent checkout. The sibling `integrations/hermes/memware/` remains the
copy you can install today by hand.

## Layout

Paths mirror hermes-agent's tree exactly, so each file lands where its path says.

| Path here | Upstream destination | New or edit |
|---|---|---|
| `plugins/memory/memware/__init__.py` | same | new |
| `plugins/memory/memware/plugin.yaml` | same | new |
| `plugins/memory/memware/config_schema.py` | same | new |
| `plugins/memory/memware/README.md` | same | new |
| `tests/plugins/memory/test_memware_provider.py` | same (directory already exists) | new |
| `website/docs/.../memory-providers.memware.md` | fragment for `memory-providers.md` | edit |

## Edits to files that already exist upstream

**1. `tools/lazy_deps.py`** — add the allowlist entry, beside the other
`memory.*` features. Without it, `ensure()` raises instead of installing, and
the provider goes dark on a sealed venv:

```python
    "memory.memware": ("memware==<published version>",),
```

Their map pins exact versions to match `pyproject.toml`'s no-ranges policy.

**2. `website/docs/user-guide/features/memory-providers.md`** — four edits,
listed in the header comment of `memory-providers.memware.md`: the frontmatter
description, the provider count, the `memory: provider:` example, and the new
`### memware` section plus its comparison-table row and profile-isolation
mention.

No other upstream file changes. Discovery is by directory scan
(`plugins/memory/__init__.py`), so there is no registry to register in.

## Design decisions a reviewer will look for

- **`is_available()` returns `True` without importing `memware`.** Gating
  availability on the import is the chicken-and-egg that stopped supermemory
  loading at all on a sealed venv — the provider never loads, so
  `initialize()` never runs, so `ensure()` never installs the package. There is
  nothing else to gate on: memware is local and takes no credentials.
- **One lazy-install chokepoint.** `_ensure_memware()` calls
  `ensure("memory.memware", prompt=False)`; every method that imports
  `memware.*` goes through it first.
- **Storage is profile-scoped by default** (`$HERMES_HOME/memware/memware.db`),
  with `$HERMES_HOME` and `~` expanded in user-supplied paths, following the
  holographic provider. A store the user has moved outside `HERMES_HOME` is
  declared through `backup_paths()` so `hermes backup` captures it.
- **`sync_turn()` is non-blocking**, per the threading contract: it appends to a
  per-session JSONL file and indexes from a byte-offset cursor on a daemon
  thread, joining any previous one first.
- **Turn capture stores text only.** The raw `messages` list is accepted and
  ignored — it can carry tool arguments and command output. Nothing leaves the
  device either way.

## Verifying

The upstream test stubs the `memware` package (as
`tests/plugins/memory/test_memory_lazy_install.py` stubs the supermemory and
mem0 SDKs), so their CI stays green without a new dependency:

```bash
pytest tests/plugins/memory/test_memware_provider.py    # in a hermes-agent checkout
```

Behaviour against a real store is covered on this side, by
`tests/test_hermes_upstream_plugin.py`, which loads the same source behind
stand-ins for the three hermes modules. It also guards against the two copies
of the provider in this repo drifting apart on the surface a user sees.

## Status

Prepared, not submitted. Still open before a PR:

- **memware published on PyPI** — the `LAZY_DEPS` entry needs a real version to
  pin, and the provider cannot lazy-install until the package resolves.
- **A soak period on a real daily-driver stack** — enough use to argue the
  provider is stable, with incidents recorded rather than assumed absent.
- **The PR text reviewed before submission.** It is public and under a real
  name; the draft lives in `docs/upstream-hermes-pr.md`.
