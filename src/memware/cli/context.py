"""`memware context`."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from memware import relevance
from memware.cli._common import AddCommand, _hook_payload, _hook_store
from memware.config import inject_k
from memware.digest import CONTEXT_TITLE, belief_line, injection_gate, resolve_project
from memware.index import search_beliefs
from memware.ledger import confirmed_sql
from memware.store import Store


def cmd_context(a: argparse.Namespace) -> int:
    """Prompt-time helper: print the beliefs whose subject the prompt names, less what the
    injection gate leaves out (memware.volatile) and, when switched on, what the relevance filter
    judges irrelevant (memware.relevance; off by default). A turn nobody typed, a background
    task's notification or a hook fired inside a subagent, gets nothing."""
    payload = _hook_payload() if a.from_hook or not a.prompt else {}
    prompt = a.prompt or str(payload.get("prompt", ""))
    agent = bool(payload.get("agent_id"))
    if not prompt.strip() or not relevance.typed(prompt, agent=agent):
        return 0
    gate = injection_gate(resolve_project(Path(str(payload.get("cwd") or os.getcwd()))))
    store = _hook_store(a.db) if a.from_hook else Store(a.db)
    if store is None:
        return 0
    with store as s:
        # Injection is not retrieval: nobody asked for these, so they must not gain activation
        # or count as used — use_count means an agent or a person retrieved the belief. Ranked
        # past k, so a left-out belief makes room for the next one rather than a shorter block.
        hits = search_beliefs(s, prompt, k=100, require_subject=True, record_use=False)
        if not hits:
            return 0
        rows = {
            r["id"]: r
            for r in s.conn.execute(
                f"SELECT *, {confirmed_sql()} FROM belief WHERE id IN ({','.join('?' * len(hits))})",
                [h.id for h in hits],
            )
        }
    admitted = [r for r in (rows[h.id] for h in hits if h.id in rows) if gate.verdict(r) is None]
    k = a.k if a.k is not None else inject_k()
    rel = relevance.settings()
    if rel.on:  # opted in: the network call happens here, after the store is closed
        picked = relevance.choose(
            prompt,
            [(r["id"], relevance.fact(r["subject"], r["relation"], r["value"])) for r in admitted],
            k,
            rel,
            harness="claude-code" if a.from_hook else "cli",
            session=str(payload.get("session_id") or "") or None,
            transcript=str(payload.get("transcript_path") or "") or None,
            agent=agent,
        )
        admitted = [admitted[i] for i in picked]
    lines = [
        belief_line(r["subject"], r["relation"], r["value"], r["valid_from"]) for r in admitted
    ][:k]
    if not lines:
        return 0
    block = CONTEXT_TITLE + "\n" + "\n".join(lines)
    if a.from_hook:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": block,
                    }
                }
            )
        )
    else:
        print(block)
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "context",
        "print the beliefs a prompt names, less the stale ones (hook-friendly)",
    )
    s.add_argument("prompt", nargs="?")
    s.add_argument("-k", type=int, default=None)  # None: inject.k (6 unless configured)
    s.add_argument("--from-hook", action="store_true")
    s.set_defaults(fn=cmd_context)
