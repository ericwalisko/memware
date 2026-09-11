"""Recall-election eval runner.

One cell = (variant, scenario, model, repeat). For each cell the fixture project's contents are
copied into a fresh directory under ``/private/tmp/gateway-work`` (outside any git repository),
``claude -p`` is spawned there with a stub ``memware`` MCP server whose ``recall`` description is
the variant under test, and the transcript is parsed for which tools the model elected to call.
Both per-cell directories are deleted when the cell ends.

    uv run --extra mcp --extra dev python eval/recall_election/run.py \
        --variant-ids synthesized,control --models sonnet opus --repeats 5 \
        --out eval/recall_election/results.jsonl

The grid is shuffled with ``--seed`` so variants do not run in blocks. Resumable: cells already
present in ``--out`` (by key) are skipped. Usage-limit and auth errors stop the whole run instead
of being recorded a hundred times. See README.md.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STUB_SERVER = HERE / "stub_server.py"
DEFAULT_FIXTURE = HERE / "fixture_project"
DEFAULT_VARIANTS = HERE / "variants"
DEFAULT_SCENARIOS = HERE / "scenarios.json"
DEFAULT_SEED = 20260911
MARKER = "[memware-eval]"  # memware.eval.MARKER: sync skips any transcript carrying it
MAX_TURNS = 12  # opus runs 4-7 tool rounds on these prompts; 3 cut most cells off
RECALL = "mcp__memware__recall"
MCP_TOOLS = [RECALL, "mcp__memware__read_session", "mcp__memware__beliefs"]
BUILTIN_TOOLS = ["Grep", "Read", "Glob"]
ISOLATION_CHOICES = ("setting-sources", "settings", "both")
DEFAULT_ISOLATION = "setting-sources"
FINAL_TEXT_CHARS = 300
SECONDS_PER_CELL = 20  # 2026-09-10 run: opus 22.4 s, sonnet 16.4 s mean per valid cell

# Per-cell isolation. The copy is the model's cwd; the meta directory (MCP config, call log) is
# its sibling, never inside the copy. Neither the root nor any name the model can see may carry
# a word that gives the eval away.
WORK_ROOT = Path("/private/tmp/gateway-work")
COPY_PREFIX = "gateway-"
META_PREFIX = "gwmeta-"
BANNED_PATH_WORDS = ("eval", "fixture", "recall", "stub", "harness")
# Bytecode embeds the source path it was compiled from (.../eval/recall_election/...), and the
# caches and VCS metadata are not part of the fixture.
COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", ".git"
)
# Claude Code loads these from the cwd and every ancestor; one in /private/tmp would reach every cell.
ANCESTOR_CONTEXT_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".claude", ".mcp.json")

# Tool inputs that name a filesystem location. Grep's ``pattern`` is a regex, Glob's is a path.
TOOL_INPUT_KEYS = ("path", "pattern", "file_path", "glob")
_WILDCARD = re.compile(r"[*?\[\]{}]")

# Same patterns as memware.derive: these mean "stop now", not "this cell failed".
_LIMIT = re.compile(
    r"hit your (session|weekly|.*) limit|usage limit|rate.?limit|too many requests", re.I
)
_AUTH = re.compile(
    r"not logged in|please run /login|run `?claude login|invalid api key|invalid x-api-key"
    r"|authentication[_ ]error|oauth token .*(expired|revoked)|token (has )?expired"
    r"|\b401\b|unauthorized|not authenticated|login required",
    re.I,
)


# ---------------------------------------------------------------------------- claude argv


@functools.cache
def claude_binary() -> str:
    path = shutil.which("claude")
    if not path:
        raise SystemExit("the `claude` CLI is not on PATH (https://claude.com/claude-code)")
    return path


@functools.cache
def claude_help() -> str:
    try:
        p = subprocess.run(
            [claude_binary(), "--help"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (p.stdout or "") + (p.stderr or "")


def supports_flag(flag: str) -> bool:
    return flag in claude_help()


def child_env() -> dict[str, str]:
    """Subscription login, no memware capture, no Claude Code auto-memory.

    Auto-memory is on by default even in a fresh directory: the init event then carries
    ``memory_paths.auto`` and the system prompt describes a memory store, which is exactly the
    kind of cue that moves a recall election. Probe 3 checks it is off.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["MEMWARE_NO_CAPTURE"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def write_mcp_config(workdir: Path, description_file: Path, call_log: Path) -> Path:
    cfg = {
        "mcpServers": {
            "memware": {
                "command": sys.executable,
                "args": [str(STUB_SERVER)],
                "env": {
                    "RECALL_DESCRIPTION_FILE": str(description_file),
                    "RECALL_CALL_LOG": str(call_log),
                },
            }
        }
    }
    path = workdir / "mcp.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def build_argv(
    prompt: str,
    model: str,
    mcp_config: Path,
    isolation: str = DEFAULT_ISOLATION,
    *,
    max_turns: int = MAX_TURNS,
    builtin_tools: list[str] | None = None,
    marker: bool = False,
) -> list[str]:
    """The exact ``claude -p`` invocation; probe.py reuses it so the probes test what runs.

    ``marker`` is off by default: the ``[memware-eval]`` system-prompt line names the eval to
    the model, and ``--no-session-persistence`` leaves no transcript for it to mark.
    """
    if isolation not in ISOLATION_CHOICES:
        raise ValueError(f"isolation must be one of {ISOLATION_CHOICES}, got {isolation!r}")
    builtin = BUILTIN_TOOLS if builtin_tools is None else builtin_tools
    argv = [
        claude_binary(),
        "-p",
        prompt,
        "--model",
        model,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(max_turns),
        "--allowedTools",
        *MCP_TOOLS,
        *builtin,
        "--tools",
        ",".join(builtin),
    ]
    if isolation in ("setting-sources", "both"):
        argv += ["--setting-sources", "project"]
    if isolation in ("settings", "both"):
        argv += ["--settings", json.dumps({"disableAllHooks": True})]
    if marker and supports_flag("--append-system-prompt"):
        argv += ["--append-system-prompt", MARKER]
    return argv


# ----------------------------------------------------------------------- per-cell isolation


def banned_words(text: str) -> list[str]:
    low = text.lower()
    return [w for w in BANNED_PATH_WORDS if w in low]


def visible_name_violations(directory: Path) -> list[str]:
    """Paths under ``directory`` (as a cell's copy would hold them) that carry a banned word."""
    bad: list[str] = []
    for dirpath, dirnames, filenames in os.walk(directory):
        ignored = COPY_IGNORE(dirpath, [*dirnames, *filenames])
        dirnames[:] = [d for d in dirnames if d not in ignored]
        for name in [*dirnames, *filenames]:
            if name in ignored:
                continue
            rel = str((Path(dirpath) / name).relative_to(directory))
            if banned_words(rel):
                bad.append(rel)
    return sorted(bad)


def assert_outside_git(directory: Path) -> None:
    """``git -C directory rev-parse`` must fail: a repository would give the model a git status."""
    try:
        p = subprocess.run(
            ["git", "-C", str(directory), "rev-parse"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"cannot run git to check {directory}: {e}") from e
    if p.returncode == 0:
        raise SystemExit(
            f"{directory} is inside a git repository; cells must not run in one (see README)"
        )


def preflight(fixture: Path, root: Path = WORK_ROOT) -> None:
    """The isolation checks that must hold before the first cell; SystemExit otherwise."""
    if banned_words(str(root)):
        raise SystemExit(f"work root {root} contains {banned_words(str(root))}")
    root.mkdir(parents=True, exist_ok=True)
    assert_outside_git(root)
    candidates = (d / name for d in (root, *root.parents) for name in ANCESTOR_CONTEXT_FILES)
    stray = [str(p) for p in candidates if p.exists()]
    if stray:
        raise SystemExit(f"{stray} would load into every cell's context; move them away")
    bad = visible_name_violations(fixture)
    if bad:
        raise SystemExit(f"fixture names the model would see carry banned words: {bad}")


def _mkdtemp(prefix: str, root: Path) -> Path:
    if banned_words(str(root)) or banned_words(prefix):
        raise ValueError(f"{root}/{prefix}* would carry a banned word")
    while True:
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
        if not banned_words(path.name):
            return path
        path.rmdir()  # the random suffix spelled one; draw again


@dataclass(frozen=True)
class CellDirs:
    copy: Path  # the model's cwd: the fixture's contents
    meta: Path  # sibling of the copy: MCP config and stub call log
    mcp_config: Path
    call_log: Path


@contextlib.contextmanager
def cell_dirs(fixture: Path, description_file: Path, root: Path = WORK_ROOT) -> Iterator[CellDirs]:
    """A fresh copy of the fixture and a sibling meta directory; both deleted on exit."""
    root.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    try:
        copy = _mkdtemp(COPY_PREFIX, root)
        made.append(copy)
        meta = _mkdtemp(META_PREFIX, root)
        made.append(meta)
        shutil.copytree(fixture, copy, ignore=COPY_IGNORE, dirs_exist_ok=True)
        bad = visible_name_violations(copy)
        if bad:
            raise RuntimeError(f"copy {copy} holds names with banned words: {bad}")
        call_log = meta / "calls.jsonl"
        cfg = write_mcp_config(meta, description_file, call_log)
        yield CellDirs(copy=copy, meta=meta, mcp_config=cfg, call_log=call_log)
    finally:
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


def inside_copy(value: str, copy: Path) -> bool:
    """Whether a path-like tool input stays in the copy.

    Absolute inputs must resolve inside it; relative ones are taken from it and must not climb
    out through ``..``. A glob is judged by its literal prefix, up to the first wildcard part;
    a ``..`` after a wildcard counts as climbing out, since ``**`` may match no directory.
    """
    parts = Path(os.path.expanduser(value.strip())).parts
    cut = next((i for i, part in enumerate(parts) if _WILDCARD.search(part)), len(parts))
    if ".." in parts[cut:]:
        return False
    literal = Path(*parts[:cut]) if cut else Path(".")
    target = literal if literal.is_absolute() else copy / literal
    return target.resolve().is_relative_to(copy.resolve())


def path_values(tool_input: dict[str, Any]) -> list[tuple[str, str]]:
    """(key, value) for each input that names a location: path, file_path, glob, Glob's pattern."""
    keys = ["path", "file_path", "glob"]
    if tool_input.get("name") == "Glob":
        keys.append("pattern")
    out = []
    for k in keys:
        v = tool_input.get(k)
        if isinstance(v, str) and v.strip():
            out.append((k, v))
    return out


def out_of_copy(tool_inputs: list[dict[str, Any]], copy: Path) -> list[str]:
    return [
        f"{ti.get('name')}.{k}={v}"
        for ti in tool_inputs
        for k, v in path_values(ti)
        if not inside_copy(v, copy)
    ]


# ------------------------------------------------------------------------- one invocation


def run_claude(
    prompt: str,
    model: str,
    description_file: Path,
    fixture: Path,
    isolation: str = DEFAULT_ISOLATION,
    timeout: float = 150.0,
    *,
    max_turns: int = MAX_TURNS,
    builtin_tools: list[str] | None = None,
    marker: bool = False,
    work_root: Path = WORK_ROOT,
) -> dict[str, Any]:
    """Spawn one ``claude -p`` in a fresh copy of ``fixture`` with its own stub server.

    Returns the raw output, the stub's call log, the copy's path and whether both per-cell
    directories were removed afterwards.
    """
    with cell_dirs(fixture, description_file, work_root) as dirs:
        argv = build_argv(
            prompt,
            model,
            dirs.mcp_config,
            isolation,
            max_turns=max_turns,
            builtin_tools=builtin_tools,
            marker=marker,
        )
        t0 = time.monotonic()
        timed_out = False
        try:
            p = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=child_env(),
                stdin=subprocess.DEVNULL,  # else claude waits for piped stdin
                cwd=str(dirs.copy),
                check=False,
            )
            rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
        except subprocess.TimeoutExpired as e:
            timed_out = True
            rc = -1
            out = (
                e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout or ""
            )
            err = (
                e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else e.stderr or ""
            )
        elapsed = time.monotonic() - t0
        calls = read_call_log(dirs.call_log)
    return {
        "argv": argv,
        "rc": rc,
        "stdout": out,
        "stderr": err,
        "elapsed_s": round(elapsed, 2),
        "timed_out": timed_out,
        "calls": calls,
        "copy_dir": str(dirs.copy),
        "meta_dir": str(dirs.meta),
        "dirs_removed": not dirs.copy.exists() and not dirs.meta.exists(),
    }


def read_call_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    calls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            calls.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return calls


# --------------------------------------------------------------------- stream-json parsing


def memory_path_fields(init: dict[str, Any] | None) -> dict[str, Any]:
    """Every non-empty value under an init-event key that names memory (``memory_paths.auto``)."""
    found: dict[str, Any] = {}

    def leaves(value: Any, key: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                leaves(v, f"{key}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                leaves(v, f"{key}[{i}]")
        elif value and not (isinstance(value, str) and not value.strip()):
            found[key] = value

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                key = f"{prefix}.{k}" if prefix else str(k)
                if "memory" in str(k).lower():
                    leaves(v, key)
                else:
                    walk(v, key)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{prefix}[{i}]")

    walk(init or {}, "")
    return found


def parse_stream(stdout: str) -> dict[str, Any]:
    """Fold ``--output-format stream-json`` lines into the fields a result row records."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            events.append(ev)

    tools_called: list[str] = []
    raw_inputs: list[dict[str, Any]] = []
    assistant_texts: list[str] = []
    init: dict[str, Any] | None = None
    assistant_msgs = 0
    result: dict[str, Any] | None = None
    rate_limit_status: str | None = None
    for ev in events:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            init = ev
        elif t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            rate_limit_status = str(info.get("status") or "") or rate_limit_status
        elif t == "assistant":
            if ev.get("parent_tool_use_id"):
                continue  # subagent chatter, not the top-level model
            assistant_msgs += 1
            msg = ev.get("message") or {}
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tools_called.append(str(block.get("name")))
                    inp = block.get("input")
                    raw_inputs.append(inp if isinstance(inp, dict) else {})
                elif block.get("type") == "text" and block.get("text"):
                    assistant_texts.append(str(block["text"]))
        elif t == "result":
            result = ev

    tool_inputs = [
        {"name": name, **{k: inp[k] for k in TOOL_INPUT_KEYS if k in inp}}
        for name, inp in zip(tools_called, raw_inputs, strict=True)
    ]

    recall_queries: list[str] = []
    for name, inp in zip(tools_called, raw_inputs, strict=True):
        if name == RECALL:
            q = inp.get("queries")
            if isinstance(q, list):
                recall_queries = [str(x) for x in q]
            elif isinstance(q, str):
                recall_queries = [q]
            break

    final_text = ""
    if result is not None and isinstance(result.get("result"), str):
        final_text = result["result"]
    elif assistant_texts:
        final_text = assistant_texts[-1]

    turns = assistant_msgs
    if result is not None and isinstance(result.get("num_turns"), int):
        turns = result["num_turns"]

    return {
        "tools_called": tools_called,
        "tool_inputs": tool_inputs,
        "first_tool": tools_called[0] if tools_called else None,
        "recall_called": RECALL in tools_called,
        "recall_count": tools_called.count(RECALL),
        "recall_queries": recall_queries,
        "n_phrasings": len(recall_queries),
        "turns": turns,
        "final_text": final_text[:FINAL_TEXT_CHARS],
        "final_text_full": final_text,
        "is_error": bool(result.get("is_error")) if result else None,
        "result_subtype": result.get("subtype") if result else None,
        "result_errors": [str(e) for e in (result.get("errors") or [])] if result else [],
        "permission_denials": len(result.get("permission_denials") or []) if result else 0,
        "rate_limit_status": rate_limit_status,
        "has_init": init is not None,
        "init_cwd": (init or {}).get("cwd"),
        "init_tools": [str(x) for x in (init or {}).get("tools") or []],
        "init_mcp_servers": list((init or {}).get("mcp_servers") or []),
        "memory_paths": memory_path_fields(init),
        "n_events": len(events),
    }


def fatal_reason(raw: dict[str, Any], parsed: dict[str, Any]) -> str | None:
    """Usage-limit or auth text in a failed spawn's output: stop the run, don't record.

    A cell that succeeded (rc 0, no ``is_error``) has only its stderr scanned. The model's
    answer is scanned only on failure: the fixture is an auth gateway, so answers routinely
    say "unauthorized", "401" or "rate limit" and would otherwise stop the run.
    """
    status = parsed.get("rate_limit_status") or ""
    if status and not status.startswith("allowed"):
        return f"usage limit: rate_limit_event status={status!r}"
    blob = raw.get("stderr") or ""
    if raw["rc"] != 0 or parsed.get("is_error"):
        # The stream's own rate_limit_event lines say "allowed"; keep them out of the regex.
        tail = [
            ln for ln in (raw.get("stdout") or "").splitlines() if '"rate_limit_event"' not in ln
        ]
        blob += "\n" + (parsed.get("final_text_full") or "") + "\n" + "\n".join(tail)[-4000:]
    if _LIMIT.search(blob):
        return "usage limit: " + _first_match(_LIMIT, blob)
    if _AUTH.search(blob):
        return "auth: " + _first_match(_AUTH, blob)
    return None


def _first_match(rx: re.Pattern[str], blob: str) -> str:
    m = rx.search(blob)
    if not m:
        return ""
    start = max(0, m.start() - 80)
    return blob[start : m.end() + 80].replace("\n", " ").strip()


# ------------------------------------------------------------------- variants & scenarios


def load_variants(directory: Path) -> dict[str, Path]:
    files = sorted(p for p in directory.glob("*.md") if p.is_file())
    if not files:
        raise SystemExit(f"no .md variants in {directory}")
    return {p.stem: p.resolve() for p in files}


def parse_ids(text: str | None) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in (text or "").split(",") if x.strip()))


def select_variants(directory: Path, ids: list[str]) -> dict[str, Path]:
    found = load_variants(directory)
    if not ids:
        return found
    missing = [i for i in ids if i not in found]
    if missing:
        raise SystemExit(f"no variant {missing} in {directory}; have {sorted(found)}")
    return {i: found[i] for i in ids}


def select_scenarios(scenarios: list[dict[str, Any]], ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return scenarios
    by_id = {s["id"]: s for s in scenarios}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"no scenario {missing}; have {sorted(by_id)}")
    return [by_id[i] for i in ids]


_TRUE = {"1", "true", "yes", "y", "recall", "positive", "pos", "call", "elect"}
_FALSE = {"0", "false", "no", "n", "no_recall", "no-recall", "negative", "neg", "skip", "none"}


def expect_recall(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise SystemExit(f"cannot read expect value {value!r} as a boolean")


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    """Accept a list or ``{"scenarios": [...]}``; each item needs id, prompt, class, expect."""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("scenarios") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise SystemExit(f"{path}: expected a non-empty list of scenarios")
    out = []
    for i, s in enumerate(items):
        if not isinstance(s, dict):
            raise SystemExit(f"{path}: scenario {i} is not an object")
        sid = s.get("id") or s.get("name") or s.get("scenario")
        prompt = s.get("prompt") or s.get("question") or s.get("query")
        if not sid or not prompt:
            raise SystemExit(f"{path}: scenario {i} needs id and prompt")
        expect = s.get("expect", s.get("expect_recall", s.get("expected")))
        if expect is None:
            raise SystemExit(f"{path}: scenario {sid!r} needs expect (should recall be called?)")
        out.append(
            {
                "id": str(sid),
                "prompt": str(prompt),
                "class": str(s.get("class") or s.get("kind") or s.get("category") or ""),
                "expect": expect_recall(expect),
            }
        )
    return out


# ------------------------------------------------------------------------------ the run


def cell_key(row: dict[str, Any]) -> tuple[str, str, str, int]:
    return (str(row["variant"]), str(row["scenario"]), str(row["model"]), int(row["repeat"]))


def build_cells(
    variants: dict[str, Path],
    scenarios: list[dict[str, Any]],
    models: list[str],
    repeats: int,
    seed: int = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """Every cell of the grid, in an order fixed by the grid's contents and ``seed`` alone.

    The cells are sorted by key and then shuffled, so a resumed run (which filters this list)
    keeps the same interleaving and the order does not depend on how ids were typed.
    """
    cells = [
        {
            "variant": vid,
            "variant_file": vfile,
            "scenario": sc["id"],
            "prompt": sc["prompt"],
            "class": sc["class"],
            "expect": sc["expect"],
            "model": model,
            "repeat": rep,
        }
        for vid, vfile in variants.items()
        for sc in scenarios
        for model in dict.fromkeys(models)
        for rep in range(repeats)
    ]
    cells.sort(key=cell_key)
    random.Random(seed).shuffle(cells)
    return cells


def load_existing(path: Path) -> set[tuple[str, str, str, int]]:
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            keys.add(cell_key(row))
        except (KeyError, TypeError, ValueError):
            continue
    return keys


def classify(
    raw: dict[str, Any], parsed: dict[str, Any], escaped: list[str], timeout: float
) -> tuple[str | None, str | None]:
    """(invalid_reason, error detail), both None for a valid cell. Reading outside the copy wins."""
    if escaped:
        return "out_of_copy", "out_of_copy: " + "; ".join(escaped)
    if raw["timed_out"]:
        return "timeout", f"timeout after {timeout:g}s"
    subtype = parsed.get("result_subtype")
    detail = "; ".join(parsed.get("result_errors") or []) or (parsed.get("final_text") or "")[:200]
    if raw["rc"] != 0:
        if subtype and subtype != "success":
            return str(subtype), f"rc {raw['rc']}, {subtype}: {detail}"
        tail = (raw.get("stderr") or "").strip()[-300:] or "no stderr"
        return f"rc {raw['rc']}", f"rc {raw['rc']}: {tail}"
    if parsed.get("is_error"):
        return str(subtype or "is_error"), f"is_error ({subtype}): {detail}"
    return None, None


def build_row(
    cell: dict[str, Any],
    raw: dict[str, Any],
    parsed: dict[str, Any],
    *,
    isolation: str = DEFAULT_ISOLATION,
    timeout: float = 150.0,
    marker: bool = False,
) -> dict[str, Any]:
    logged = [str(c.get("tool")) for c in raw["calls"]]
    recall_logged = "recall" in logged
    recall_count_stub = logged.count("recall")
    escaped = out_of_copy(parsed["tool_inputs"], Path(raw["copy_dir"]))
    reason, error = classify(raw, parsed, escaped, timeout)
    return {
        "variant": cell["variant"],
        "scenario": cell["scenario"],
        "class": cell["class"],
        "expect": cell["expect"],
        "model": cell["model"],
        "repeat": cell["repeat"],
        "tools_called": parsed["tools_called"],
        "tool_inputs": parsed["tool_inputs"],
        "first_tool": parsed["first_tool"],
        "recall_called": parsed["recall_called"],
        "recall_logged": recall_logged,
        "recall_mismatch": parsed["recall_called"] != recall_logged,
        "recall_count_transcript": parsed["recall_count"],
        "recall_count_stub": recall_count_stub,
        "count_mismatch": parsed["recall_count"] != recall_count_stub,
        "stub_calls": logged,
        "recall_queries": parsed["recall_queries"],
        "n_phrasings": parsed["n_phrasings"],
        "turns": parsed["turns"],
        "permission_denials": parsed["permission_denials"],
        "result_subtype": parsed["result_subtype"],
        "elapsed_s": raw["elapsed_s"],
        "final_text": parsed["final_text"],
        "rc": raw["rc"],
        "error": error,
        "invalid_reason": reason,
        "out_of_copy": escaped,
        "valid": reason is None,
        "copy_dir": raw["copy_dir"],
        "dirs_removed": raw["dirs_removed"],
        "memory_paths": parsed["memory_paths"],
        "isolation": isolation,
        "marker": marker,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def run_cell(cell: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Return (row, fatal_reason)."""
    raw = run_claude(
        cell["prompt"],
        cell["model"],
        cell["variant_file"],
        args.fixture,
        args.isolation,
        timeout=args.timeout,
        max_turns=args.max_turns,
        marker=args.marker,
    )
    parsed = parse_stream(raw["stdout"])
    if args.raw_dir:
        args.raw_dir.mkdir(parents=True, exist_ok=True)
        stem = "__".join(str(cell[k]) for k in ("variant", "scenario", "model", "repeat"))
        (args.raw_dir / f"{stem}.stdout.jsonl").write_text(raw["stdout"], encoding="utf-8")
        (args.raw_dir / f"{stem}.stderr.txt").write_text(raw["stderr"], encoding="utf-8")
    row = build_row(
        cell, raw, parsed, isolation=args.isolation, timeout=args.timeout, marker=args.marker
    )
    return row, fatal_reason(raw, parsed)


def add_marker_flags(ap: argparse.ArgumentParser) -> None:
    group = ap.add_mutually_exclusive_group()
    group.add_argument(
        "--marker",
        dest="marker",
        action="store_true",
        help=f"send --append-system-prompt {MARKER} (off by default: it names the eval to the model)",
    )
    group.add_argument(
        "--no-marker",
        dest="marker",
        action="store_false",
        help="the default; kept for old commands",
    )
    ap.set_defaults(marker=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--variants", type=Path, default=DEFAULT_VARIANTS, help="dir of <id>.md")
    ap.add_argument(
        "--variant-ids", default=None, help="comma list of variant stems to run (default: all)"
    )
    ap.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS, help="scenarios JSON")
    ap.add_argument(
        "--scenario-ids", default=None, help="comma list of scenario ids to run (default: all)"
    )
    ap.add_argument("--models", nargs="+", required=True, help="claude --model values")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="shuffles the cell order")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path("results.jsonl"))
    ap.add_argument("--timeout", type=float, default=150.0, help="seconds per cell")
    ap.add_argument(
        "--max-turns",
        type=int,
        default=MAX_TURNS,
        help="claude --max-turns; cells that hit it end in error_max_turns and score as invalid",
    )
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE, help="copied into each cwd")
    ap.add_argument(
        "--isolation",
        choices=ISOLATION_CHOICES,
        default=DEFAULT_ISOLATION,
        help="how user settings/hooks are excluded (see README; probe.py verifies)",
    )
    add_marker_flags(ap)
    ap.add_argument("--limit", type=int, default=0, help="run at most N pending cells (smoke)")
    ap.add_argument(
        "--raw-dir", type=Path, default=None, help="also save each cell's raw stdout/stderr"
    )
    ap.add_argument("--dry-run", action="store_true", help="list pending cells and exit")
    args = ap.parse_args(argv)

    args.fixture = args.fixture.resolve()
    if not args.fixture.is_dir():
        raise SystemExit(f"fixture project {args.fixture} is not a directory")
    if not STUB_SERVER.exists():
        raise SystemExit(f"missing {STUB_SERVER}")
    variants = select_variants(args.variants, parse_ids(args.variant_ids))
    scenarios = select_scenarios(load_scenarios(args.scenarios), parse_ids(args.scenario_ids))
    done = load_existing(args.out)

    grid = build_cells(variants, scenarios, args.models, args.repeats, args.seed)
    cells = [c for c in grid if cell_key(c) not in done]
    n_done_before = len(grid) - len(cells)
    if args.limit > 0:
        cells = cells[: args.limit]
    est = len(cells) * SECONDS_PER_CELL / max(1, args.parallel)
    print(
        f"{len(grid)} cells in the grid, {n_done_before} already in {args.out}, {len(cells)} to "
        f"run ({len(variants)} variants x {len(scenarios)} scenarios x "
        f"{len(dict.fromkeys(args.models))} models x {args.repeats} repeats, seed {args.seed}); "
        f"~{est / 60:.1f} min at ~{SECONDS_PER_CELL} s/cell, parallel {args.parallel}",
        flush=True,
    )
    if args.dry_run:
        for c in cells:
            print(f"  {c['variant']} / {c['scenario']} / {c['model']} / {c['repeat']}")
        return 0
    if not cells:
        return 0
    preflight(args.fixture)
    if args.marker and not supports_flag("--append-system-prompt"):
        print("note: this claude has no --append-system-prompt; marker not sent", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    stop = threading.Event()
    n_done = n_err = 0
    fatal: str | None = None
    t0 = time.monotonic()

    def work(cell: dict[str, Any]) -> tuple[dict[str, Any], str | None] | None:
        if stop.is_set():
            return None
        return run_cell(cell, args)

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        pending: set[Future[Any]] = {pool.submit(work, c) for c in cells}
        try:
            while pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in finished:
                    res = fut.result()
                    if res is None:
                        continue
                    row, reason = res
                    if reason and not stop.is_set():
                        fatal = reason
                        stop.set()
                        for f in pending:
                            f.cancel()
                        break
                    if reason:
                        continue
                    with lock:
                        with args.out.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        n_done += 1
                        if not row["valid"]:
                            n_err += 1
                        if n_done % 10 == 0:
                            rate = (time.monotonic() - t0) / n_done
                            print(
                                f"[{n_done}/{len(cells)}] {n_err} invalid, "
                                f"{rate:.1f} s/cell, ~{rate * (len(cells) - n_done) / 60:.1f} min left",
                                flush=True,
                            )
                if stop.is_set():
                    break
        except KeyboardInterrupt:
            stop.set()
            for f in pending:
                f.cancel()
            print("\ninterrupted; rerun the same command to resume", flush=True)
            return 130

    print(f"done: {n_done} cells written to {args.out}, {n_err} invalid", flush=True)
    if fatal:
        print(
            f"\nSTOPPED: claude reported {fatal}\n"
            "Nothing was recorded for the failing cell. Fix the cause (wait out the limit, "
            "`claude login`) and rerun the same command; finished cells are kept.",
            file=sys.stderr,
            flush=True,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
