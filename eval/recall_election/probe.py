"""Isolation probes for the recall-election eval.

Two ``claude -p`` spawns with exactly the argv run.py uses (same fixture cwd, stub server,
isolation flag), each answering one question about its own context:

1. tool list   PASS iff the ``mcp__memware__`` tools the model lists are exactly recall,
               read_session and beliefs and no other ``mcp__`` name appears (the real memware
               server registered at user scope must be absent). The init event's tool list is
               checked the same way as corroborating evidence.
2. Known facts PASS iff the model answers NO to whether its context holds a block starting
               with "Known facts" (what the memware UserPromptSubmit hook injects).

    uv run --extra mcp --extra dev python eval/recall_election/probe.py \
        --variant eval/recall_election/variants/baseline.md --model opus

Exit 0 on PASS/PASS, 1 otherwise. The last stdout line is a JSON summary.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import (
    DEFAULT_FIXTURE,
    DEFAULT_ISOLATION,
    ISOLATION_CHOICES,
    fatal_reason,
    neutral_name,
    parse_stream,
    run_claude,
)

EXPECTED_MEMWARE = {"recall", "read_session", "beliefs"}
PROMPT_TOOLS = "List the names of every tool available to you, one per line, nothing else."
PROMPT_FACTS = (
    "Does your context contain a block that starts with the words Known facts? "
    "Answer only YES or NO."
)
PROMPT_WHERE = (
    "Name the project directory you are working in and say in one sentence what this "
    "codebase is for. Do not use any tools; answer from what you already have."
)
_MCP_NAME = re.compile(r"mcp__[A-Za-z0-9_\-]+")
# Probe 3a: any auto-memory path reported by the init event. The memware/Claude Code
# auto-memory lives under a `memory/` directory with a MEMORY.md index; a path like that in
# the init event means the cell inherited a memory store and every number below it is moot.
_MEMORY_PATH = re.compile(r"MEMORY\.md|[/\\]memory[/\\]|auto[_-]?memory|\.memware", re.I)
_MEMORY_KEY = re.compile(r"memor(y|ies)", re.I)
# Probe 3b: words that would mean the model can read what it is part of off its own context.
# Bare "eval" is deliberately NOT here: a truthful "this is not an evaluation" contains it.
_SELF_AWARE = re.compile(r"fixture|recall[ _-]?election|stub[ _-]?server|memware-eval", re.I)


def mcp_split(names: list[str]) -> tuple[set[str], set[str]]:
    """(memware tool short names, other mcp__ names)."""
    memware: set[str] = set()
    other: set[str] = set()
    for n in names:
        if n.startswith("mcp__memware__"):
            memware.add(n.removeprefix("mcp__memware__"))
        elif n.startswith("mcp__"):
            other.add(n)
    return memware, other


def check_tools(text: str, init_tools: list[str]) -> tuple[bool, dict[str, Any]]:
    listed = _MCP_NAME.findall(text)
    memware, other = mcp_split(listed)
    answer_ok = memware == EXPECTED_MEMWARE and not other
    init_memware, init_other = mcp_split(init_tools)
    init_ok = True if not init_tools else (init_memware == EXPECTED_MEMWARE and not init_other)
    return answer_ok and init_ok, {
        "listed_memware": sorted(memware),
        "listed_other_mcp": sorted(other),
        "answer_ok": answer_ok,
        "init_tools": init_tools,
        "init_ok": init_ok,
    }


def check_known_facts(text: str) -> tuple[bool, dict[str, Any]]:
    norm = re.sub(r"[^A-Z]", " ", text.strip().upper()).split()
    first = norm[0] if norm else ""
    return first == "NO", {"first_word": first}


def check_no_memory(init_event: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """PASS iff the init event reports no auto-memory path (by key name or by value)."""
    hits: list[str] = []
    for key, value in (init_event or {}).items():
        blob = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if _MEMORY_KEY.search(str(key)) and value not in (None, [], {}, "", False):
            hits.append(f"{key}={blob[:120]}")
        elif _MEMORY_PATH.search(blob):
            hits.append(f"{key}={blob[:120]}")
    return not hits, {"memory_hits": hits, "init_keys": sorted(init_event or {})}


def check_self_aware(text: str, run_cwd: str) -> tuple[bool, dict[str, Any]]:
    """PASS iff the answer names no eval/stub/fixture, and the cwd itself leaks nothing."""
    answer_hits = sorted({m.group(0).lower() for m in _SELF_AWARE.finditer(text)})
    cwd_ok = neutral_name(Path(run_cwd))
    return (not answer_hits and cwd_ok), {
        "answer_hits": answer_hits,
        "run_cwd": run_cwd,
        "cwd_neutral": cwd_ok,
    }


def probe(
    prompt: str,
    variant: Path,
    model: str,
    fixture: Path,
    isolation: str,
    timeout: float,
    marker: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = run_claude(prompt, model, variant, fixture, isolation, timeout=timeout, marker=marker)
    parsed = parse_stream(raw["stdout"])
    reason = fatal_reason(raw, parsed)
    if reason:
        raise SystemExit(f"probe aborted: claude reported {reason}")
    if raw["timed_out"] or raw["rc"] != 0:
        raise SystemExit(
            f"probe aborted: claude rc {raw['rc']} timed_out={raw['timed_out']}\n"
            f"{(raw['stderr'] or raw['stdout'])[-800:]}"
        )
    return raw, parsed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--variant", type=Path, required=True, help="a variant .md (recall description)"
    )
    ap.add_argument("--model", required=True)
    ap.add_argument("--isolation", choices=ISOLATION_CHOICES, default=DEFAULT_ISOLATION)
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--timeout", type=float, default=150.0)
    ap.add_argument("--no-marker", action="store_true")
    args = ap.parse_args(argv)
    variant = args.variant.resolve()
    fixture = args.fixture.resolve()
    if not variant.is_file():
        raise SystemExit(f"{variant} is not a file")
    if not fixture.is_dir():
        raise SystemExit(f"{fixture} is not a directory")

    common = (variant, args.model, fixture, args.isolation, args.timeout, not args.no_marker)
    print(f"isolation={args.isolation} model={args.model} fixture={fixture}", flush=True)

    raw1, p1 = probe(PROMPT_TOOLS, *common)
    ok1, ev1 = check_tools(p1["final_text_full"], p1["init_tools"])
    print(f"probe 1 (tool list): {'PASS' if ok1 else 'FAIL'}")
    print(f"  memware tools listed: {ev1['listed_memware']} (need {sorted(EXPECTED_MEMWARE)})")
    print(f"  other mcp__ listed:   {ev1['listed_other_mcp']} (need none)")
    print(f"  init event tools:     {ev1['init_tools']} -> {'ok' if ev1['init_ok'] else 'FAIL'}")
    print(f"  mcp servers (init):   {p1['init_mcp_servers']}")
    print(f"  tools model called:   {p1['tools_called']}")
    print("  answer: " + p1["final_text_full"].strip().replace("\n", " | ")[:600])

    raw2, p2 = probe(PROMPT_FACTS, *common)
    ok2, ev2 = check_known_facts(p2["final_text_full"])
    print(f"probe 2 (Known facts): {'PASS' if ok2 else 'FAIL'}")
    print(f"  first word: {ev2['first_word']!r} (need 'NO')")
    print(f"  tools model called:   {p2['tools_called']}")
    print("  answer: " + p2["final_text_full"].strip().replace("\n", " | ")[:300])

    raw3, p3 = probe(PROMPT_WHERE, *common)
    ok3a, ev3a = check_no_memory(p3["init_event"])
    ok3b, ev3b = check_self_aware(p3["final_text_full"], raw3["run_cwd"])
    ok3 = ok3a and ok3b
    print(f"probe 3 (no memory / not self-aware): {'PASS' if ok3 else 'FAIL'}")
    print(f"  init-event memory paths: {ev3a['memory_hits']} (need none)")
    print(f"  cwd: {ev3b['run_cwd']} -> {'neutral' if ev3b['cwd_neutral'] else 'LEAKS'}")
    print(f"  eval words in answer:    {ev3b['answer_hits']} (need none)")
    print("  answer: " + p3["final_text_full"].strip().replace("\n", " | ")[:300])

    verdict = ok1 and ok2 and ok3
    summary = {
        "isolation": args.isolation,
        "model": args.model,
        "probe1": "PASS" if ok1 else "FAIL",
        "probe2": "PASS" if ok2 else "FAIL",
        "probe3": "PASS" if ok3 else "FAIL",
        "evidence": {
            "probe1": {**ev1, "answer": p1["final_text_full"][:600]},
            "probe2": {**ev2, "answer": p2["final_text_full"][:300]},
            "probe3": {**ev3a, **ev3b, "answer": p3["final_text_full"][:300]},
            "argv_flags": [a for a in raw1["argv"][3:] if a.startswith("--")],
        },
        "elapsed_s": [raw1["elapsed_s"], raw2["elapsed_s"], raw3["elapsed_s"]],
    }
    print(f"RESULT: {'PASS' if verdict else 'FAIL'}")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
