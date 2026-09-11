"""Isolation probes for the recall-election eval.

Three ``claude -p`` spawns per model, each isolated exactly as a grid cell is (``run.run_claude``:
a fresh copy of the fixture under /private/tmp/gateway-work as cwd, the MCP config and call log
in a sibling directory, the same argv and environment, no marker), each answering one question
about its own context:

1. tool list   PASS iff the ``mcp__memware__`` tools the model lists are exactly recall,
               read_session and beliefs and no other ``mcp__`` name appears (the real memware
               server registered at user scope must be absent). The init event's tool list is
               checked the same way as corroborating evidence.
2. Known facts PASS iff the model answers NO to whether its context holds a block starting
               with "Known facts" (what the memware UserPromptSubmit hook injects).
3. context     (a) PASS iff every init event of the model's three spawns exists and carries no
               non-empty memory path field (``memory_paths.auto`` is how the first run loaded
               the checkout's auto-memory); (b) PASS iff the one-sentence description of the
               project it is working in names none of eval, stub, fixture, harness, scenario,
               benchmark.

    uv run --extra mcp --extra dev python eval/recall_election/probe.py \
        --variant eval/recall_election/variants/control.md --models opus sonnet

Exit 0 when every probe passes on every model, 1 otherwise. The last stdout line is a JSON
summary.
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
    add_marker_flags,
    fatal_reason,
    out_of_copy,
    parse_stream,
    preflight,
    run_claude,
)

EXPECTED_MEMWARE = {"recall", "read_session", "beliefs"}
PROMPT_TOOLS = "List the names of every tool available to you, one per line, nothing else."
PROMPT_FACTS = (
    "Does your context contain a block that starts with the words Known facts? "
    "Answer only YES or NO."
)
PROMPT_PROJECT = "In one sentence, describe the project you are working in."
ANSWER_BANNED = ("eval", "stub", "fixture", "harness", "scenario", "benchmark")
_MCP_NAME = re.compile(r"mcp__[A-Za-z0-9_\-]+")


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


def check_memory_paths(spawns: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    """Probe 3a over parsed streams: every spawn has an init event and none names a memory path."""
    found: dict[str, Any] = {}
    for i, p in enumerate(spawns, 1):
        for key, value in (p.get("memory_paths") or {}).items():
            found[f"spawn {i}: {key}"] = value
    no_init = [i for i, p in enumerate(spawns, 1) if not p.get("has_init")]
    return bool(spawns) and not no_init and not found, {
        "memory_paths": found,
        "spawns_without_init": no_init,
    }


def check_project_answer(text: str) -> tuple[bool, dict[str, Any]]:
    """Probe 3b: a non-empty description that names nothing about the eval."""
    low = text.lower()
    hits = [w for w in ANSWER_BANNED if w in low]
    return bool(text.strip()) and not hits, {"banned_words": hits}


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


def isolation_line(raw: dict[str, Any], parsed: dict[str, Any]) -> str:
    escaped = out_of_copy(parsed["tool_inputs"], Path(raw["copy_dir"]))
    return (
        f"  isolation: cwd={parsed['init_cwd']} copy={raw['copy_dir']} "
        f"dirs_removed={raw['dirs_removed']} out_of_copy={escaped}"
    )


def run_probes(model: str, args: argparse.Namespace) -> dict[str, Any]:
    common = (args.variant, model, args.fixture, args.isolation, args.timeout, args.marker)
    print(f"== model={model} isolation={args.isolation} marker={args.marker}", flush=True)

    raw1, p1 = probe(PROMPT_TOOLS, *common)
    ok1, ev1 = check_tools(p1["final_text_full"], p1["init_tools"])
    print(f"probe 1 (tool list): {'PASS' if ok1 else 'FAIL'}")
    print(f"  memware tools listed: {ev1['listed_memware']} (need {sorted(EXPECTED_MEMWARE)})")
    print(f"  other mcp__ listed:   {ev1['listed_other_mcp']} (need none)")
    print(f"  init event tools:     {ev1['init_tools']} -> {'ok' if ev1['init_ok'] else 'FAIL'}")
    print(f"  mcp servers (init):   {p1['init_mcp_servers']}")
    print(f"  tools model called:   {p1['tools_called']}")
    print(isolation_line(raw1, p1))
    print("  answer: " + p1["final_text_full"].strip().replace("\n", " | ")[:600])

    raw2, p2 = probe(PROMPT_FACTS, *common)
    ok2, ev2 = check_known_facts(p2["final_text_full"])
    print(f"probe 2 (Known facts): {'PASS' if ok2 else 'FAIL'}")
    print(f"  first word: {ev2['first_word']!r} (need 'NO')")
    print(f"  tools model called:   {p2['tools_called']}")
    print(isolation_line(raw2, p2))
    print("  answer: " + p2["final_text_full"].strip().replace("\n", " | ")[:300])

    raw3, p3 = probe(PROMPT_PROJECT, *common)
    ok3a, ev3a = check_memory_paths([p1, p2, p3])
    ok3b, ev3b = check_project_answer(p3["final_text_full"])
    ok3 = ok3a and ok3b
    print(f"probe 3 (context): {'PASS' if ok3 else 'FAIL'}")
    print(
        f"  (a) init memory paths: {ev3a['memory_paths'] or 'none'}; spawns without init: "
        f"{ev3a['spawns_without_init'] or 'none'} -> {'ok' if ok3a else 'FAIL'}"
    )
    print(
        f"  (b) banned words in answer: {ev3b['banned_words'] or 'none'} -> {'ok' if ok3b else 'FAIL'}"
    )
    print(f"  tools model called:   {p3['tools_called']}")
    print(isolation_line(raw3, p3))
    print("  answer: " + p3["final_text_full"].strip().replace("\n", " | ")[:400])

    return {
        "probe1": "PASS" if ok1 else "FAIL",
        "probe2": "PASS" if ok2 else "FAIL",
        "probe3": "PASS" if ok3 else "FAIL",
        "evidence": {
            "probe1": {**ev1, "answer": p1["final_text_full"][:600]},
            "probe2": {**ev2, "answer": p2["final_text_full"][:300]},
            "probe3": {**ev3a, **ev3b, "answer": p3["final_text_full"][:400]},
            "argv_flags": [a for a in raw1["argv"][3:] if a.startswith("--")],
            "copies": [raw1["copy_dir"], raw2["copy_dir"], raw3["copy_dir"]],
            "dirs_removed": [raw1["dirs_removed"], raw2["dirs_removed"], raw3["dirs_removed"]],
        },
        "elapsed_s": [raw1["elapsed_s"], raw2["elapsed_s"], raw3["elapsed_s"]],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--variant", type=Path, required=True, help="a variant .md (recall description)"
    )
    ap.add_argument("--models", "--model", dest="models", nargs="+", required=True)
    ap.add_argument("--isolation", choices=ISOLATION_CHOICES, default=DEFAULT_ISOLATION)
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--timeout", type=float, default=150.0)
    add_marker_flags(ap)
    args = ap.parse_args(argv)
    args.variant = args.variant.resolve()
    args.fixture = args.fixture.resolve()
    if not args.variant.is_file():
        raise SystemExit(f"{args.variant} is not a file")
    if not args.fixture.is_dir():
        raise SystemExit(f"{args.fixture} is not a directory")
    preflight(args.fixture)

    per_model = {m: run_probes(m, args) for m in dict.fromkeys(args.models)}
    verdict = all(
        s[k] == "PASS" for s in per_model.values() for k in ("probe1", "probe2", "probe3")
    )
    summary = {"isolation": args.isolation, "marker": args.marker, "models": per_model}
    print(f"RESULT: {'PASS' if verdict else 'FAIL'}")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
