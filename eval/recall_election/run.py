"""Recall-election eval runner.

One cell = (variant, scenario, model, repeat). For each cell, ``claude -p`` is spawned inside
the fixture project with a stub ``memware`` MCP server whose ``recall`` description is the
variant under test, and the transcript is parsed for which tools the model elected to call.

    uv run --extra mcp --extra dev python eval/recall_election/run.py \
        --variants eval/recall_election/variants --scenarios eval/recall_election/scenarios.json \
        --models sonnet opus --repeats 3 --out results.jsonl

Resumable: cells already present in ``--out`` (by key) are skipped. Usage-limit and auth
errors stop the whole run instead of being recorded a hundred times. See README.md.
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
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STUB_SERVER = HERE / "stub_server.py"
DEFAULT_FIXTURE = HERE / "fixture_project"
MARKER = "[memware-eval]"  # memware.eval.MARKER: sync skips any transcript carrying it
MAX_TURNS = 12  # opus runs 4-7 tool rounds on these prompts; 3 cut most cells off
MCP_TOOLS = ["mcp__memware__recall", "mcp__memware__read_session", "mcp__memware__beliefs"]
BUILTIN_TOOLS = ["Grep", "Read", "Glob"]
ISOLATION_CHOICES = ("setting-sources", "settings", "both")
DEFAULT_ISOLATION = "setting-sources"
FINAL_TEXT_CHARS = 300
DEFAULT_SEED = 1729  # cell order is shuffled across variants; same seed => same order

# Every cell runs on a throwaway copy of the fixture under a NEUTRAL name: nothing in the
# cwd, the temp prefix or the mcp-config path may say "eval", "fixture" or "recall", or the
# model can read what it is part of off its own working directory (probe 3).
COPY_PREFIX = "wkspc-"
COPY_NAME = "gateway"
BANNED_PATH_WORDS = ("eval", "fixture", "recall")
# Tool inputs worth recording per row; the values are also what the out-of-copy check reads.
RECORDED_INPUTS = ("path", "pattern", "file_path", "glob", "notebook_path")

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
    """Subscription login (no API key, as memware.derive does) and no memware capture."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["MEMWARE_NO_CAPTURE"] = "1"
    return env


def outside_git_repo(path: Path) -> bool:
    """True when no ancestor of ``path`` holds a .git — the copy must not be in a checkout."""
    return not any((p / ".git").exists() for p in [path, *path.parents])


def neutral_name(path: Path) -> bool:
    return not any(w in str(path).lower() for w in BANNED_PATH_WORDS)


@contextlib.contextmanager
def neutral_copy(src: Path) -> Iterator[Path]:
    """A fresh copy of ``src`` under a neutral name, outside any git repo, deleted after."""
    root = Path(tempfile.mkdtemp(prefix=COPY_PREFIX)).resolve()
    try:
        if not outside_git_repo(root):
            raise SystemExit(f"temp copy {root} is inside a git repo; set TMPDIR elsewhere")
        dst = root / COPY_NAME
        shutil.copytree(
            src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv")
        )
        if not neutral_name(dst):
            raise SystemExit(f"copy path {dst} leaks an eval word; TMPDIR must be neutral too")
        yield dst
    finally:
        shutil.rmtree(root, ignore_errors=True)


def claude_project_dir(cwd: Path) -> Path:
    """Where ``claude`` would put a transcript for this cwd (non-slash chars -> '-')."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / slug


def sweep_project_dir(cwd: Path) -> dict[str, Any]:
    """Assert no transcript was written for this cwd, then remove the directory.

    ``--no-session-persistence`` should mean no ``*.jsonl`` ever appears; the directory
    itself is still created. Anything else found is reported and the directory is KEPT,
    because a transcript is evidence of a leak and must not be destroyed before it is read.
    """
    d = claude_project_dir(cwd)
    if not d.exists():
        return {"dir": str(d), "existed": False, "jsonl": [], "removed": False}
    jsonl = sorted(str(p) for p in d.rglob("*.jsonl"))
    removed = False
    if not jsonl:
        shutil.rmtree(d, ignore_errors=True)
        removed = not d.exists()
    return {"dir": str(d), "existed": True, "jsonl": jsonl, "removed": removed}


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
    marker: bool = True,
) -> list[str]:
    """The exact ``claude -p`` invocation; probe.py reuses it so the probes test what runs."""
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


# ------------------------------------------------------------------------- one invocation


def run_claude(
    prompt: str,
    model: str,
    description_file: Path,
    cwd: Path,
    isolation: str = DEFAULT_ISOLATION,
    timeout: float = 150.0,
    *,
    max_turns: int = MAX_TURNS,
    builtin_tools: list[str] | None = None,
    marker: bool = True,
    isolate: bool = True,
) -> dict[str, Any]:
    """Spawn one ``claude -p`` with a fresh stub server; return raw output plus the call log.

    With ``isolate`` (the default) the cwd is a throwaway neutral-named copy of ``cwd``
    made outside any git repo and deleted when the cell ends.
    """
    with contextlib.ExitStack() as stack:
        run_cwd = Path(stack.enter_context(neutral_copy(cwd))) if isolate else cwd.resolve()
        tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix=COPY_PREFIX))
        workdir = Path(tmp)
        call_log = workdir / "calls.jsonl"
        cfg = write_mcp_config(workdir, description_file, call_log)
        argv = build_argv(
            prompt,
            model,
            cfg,
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
                cwd=str(run_cwd),
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
        calls = read_call_log(call_log)
        sweep = sweep_project_dir(run_cwd) if isolate else {}
    return {
        "argv": argv,
        "rc": rc,
        "stdout": out,
        "stderr": err,
        "elapsed_s": round(elapsed, 2),
        "timed_out": timed_out,
        "calls": calls,
        "run_cwd": str(run_cwd),
        "project_dir_sweep": sweep,
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
    tool_inputs: list[dict[str, Any]] = []
    assistant_texts: list[str] = []
    init_tools: list[str] = []
    init_mcp: list[dict[str, Any]] = []
    init_event: dict[str, Any] = {}
    assistant_msgs = 0
    result: dict[str, Any] | None = None
    rate_limit_status: str | None = None
    for ev in events:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            init_tools = [str(x) for x in ev.get("tools") or []]
            init_mcp = list(ev.get("mcp_servers") or [])
            init_event = ev
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
                    tool_inputs.append(inp if isinstance(inp, dict) else {})
                elif block.get("type") == "text" and block.get("text"):
                    assistant_texts.append(str(block["text"]))
        elif t == "result":
            result = ev

    recall_queries: list[str] = []
    for name, inp in zip(tools_called, tool_inputs, strict=True):
        if name == "mcp__memware__recall":
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

    recorded_inputs = [
        {
            "tool": name,
            **{
                k: str(v)
                for k, v in inp.items()
                if k in RECORDED_INPUTS and isinstance(v, (str, int, float))
            },
        }
        for name, inp in zip(tools_called, tool_inputs, strict=True)
    ]

    return {
        "tools_called": tools_called,
        "tool_inputs": recorded_inputs,
        "first_tool": tools_called[0] if tools_called else None,
        "recall_called": "mcp__memware__recall" in tools_called,
        "recall_queries": recall_queries,
        "n_phrasings": len(recall_queries),
        "turns": turns,
        "final_text": final_text[:FINAL_TEXT_CHARS],
        "final_text_full": final_text,
        "is_error": bool(result.get("is_error")) if result else None,
        "result_subtype": result.get("subtype") if result else None,
        "permission_denials": len(result.get("permission_denials") or []) if result else 0,
        "rate_limit_status": rate_limit_status,
        "init_tools": init_tools,
        "init_mcp_servers": init_mcp,
        "init_event": init_event,
        "n_events": len(events),
    }


_PATH_KEYS = ("path", "file_path", "notebook_path")


def out_of_copy(recorded_inputs: list[dict[str, Any]], run_cwd: str) -> list[str]:
    """Absolute tool paths that fall outside the cell's own copy — the cell is then invalid.

    Relative paths resolve inside the cwd by construction and are not flagged; ``..`` that
    climbs out of the copy is, because the resolved path leaves the tree.
    """
    root = Path(run_cwd).resolve()
    escaped: list[str] = []
    for call in recorded_inputs:
        for key in _PATH_KEYS:
            raw = call.get(key)
            if not raw:
                continue
            p = Path(str(raw))
            resolved = (p if p.is_absolute() else root / p).resolve()
            if resolved != root and root not in resolved.parents:
                escaped.append(f"{call.get('tool')}:{key}={raw}")
    return escaped


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
        marker=not args.no_marker,
        isolate=not args.no_isolate_copies,
    )
    parsed = parse_stream(raw["stdout"])
    escaped = out_of_copy(parsed["tool_inputs"], raw["run_cwd"])
    leaked = list(raw.get("project_dir_sweep", {}).get("jsonl") or [])
    if args.raw_dir:
        args.raw_dir.mkdir(parents=True, exist_ok=True)
        stem = "__".join(str(cell[k]) for k in ("variant", "scenario", "model", "repeat"))
        (args.raw_dir / f"{stem}.stdout.jsonl").write_text(raw["stdout"], encoding="utf-8")
        (args.raw_dir / f"{stem}.stderr.txt").write_text(raw["stderr"], encoding="utf-8")
    logged = [c.get("tool") for c in raw["calls"]]
    recall_logged = "recall" in logged
    error: str | None = None
    if raw["timed_out"]:
        error = f"timeout after {args.timeout}s"
    elif raw["rc"] != 0:
        error = f"rc {raw['rc']}: {(raw['stderr'] or raw['stdout'])[-300:].strip()}"
    elif parsed["is_error"]:
        error = f"is_error ({parsed['result_subtype']}): {parsed['final_text'][:200]}"
    elif escaped:
        error = "tool touched a path outside the cell copy: " + "; ".join(escaped[:5])
    elif leaked:
        error = "transcript written under ~/.claude/projects: " + "; ".join(leaked[:3])
    row = {
        "variant": cell["variant"],
        "scenario": cell["scenario"],
        "class": cell["class"],
        "expect": cell["expect"],
        "model": cell["model"],
        "repeat": cell["repeat"],
        "tools_called": parsed["tools_called"],
        "tool_inputs": parsed["tool_inputs"],
        "out_of_copy": escaped,
        "project_dir_jsonl": leaked,
        "project_dir_removed": bool(raw.get("project_dir_sweep", {}).get("removed")),
        "first_tool": parsed["first_tool"],
        "recall_called": parsed["recall_called"],
        "recall_logged": recall_logged,
        "recall_mismatch": parsed["recall_called"] != recall_logged,
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
        "valid": error is None,
        "isolation": args.isolation,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return row, fatal_reason(raw, parsed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--variants", type=Path, required=True, help="dir of <id>.md descriptions")
    ap.add_argument("--scenarios", type=Path, required=True, help="scenarios JSON")
    ap.add_argument("--models", nargs="+", required=True, help="claude --model values")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path("results.jsonl"))
    ap.add_argument("--timeout", type=float, default=150.0, help="seconds per cell")
    ap.add_argument(
        "--max-turns",
        type=int,
        default=MAX_TURNS,
        help="claude --max-turns; cells that hit it end in error_max_turns and score as invalid",
    )
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE, help="cwd for claude")
    ap.add_argument(
        "--isolation",
        choices=ISOLATION_CHOICES,
        default=DEFAULT_ISOLATION,
        help="how user settings/hooks are excluded (see README; probe.py verifies)",
    )
    ap.add_argument(
        "--no-marker", action="store_true", help=f"omit --append-system-prompt {MARKER}"
    )
    ap.add_argument("--limit", type=int, default=0, help="run at most N pending cells (smoke)")
    ap.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="shuffle cell order across variants"
    )
    ap.add_argument(
        "--no-isolate-copies",
        action="store_true",
        help="run in the fixture itself instead of a throwaway neutral copy (debugging only)",
    )
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
    variants = load_variants(args.variants)
    scenarios = load_scenarios(args.scenarios)
    done = load_existing(args.out)

    cells = []
    for vid, vfile in variants.items():
        for sc in scenarios:
            for model in args.models:
                for rep in range(args.repeats):
                    cells.append(
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
                    )
    total = len(variants) * len(scenarios) * len(args.models) * args.repeats
    # Shuffle across variants with a fixed seed: a variant is never run as one contiguous
    # block, so drift in the service over a long run cannot land on one variant. The shuffle
    # is applied to the WHOLE grid before finished cells are dropped, so a resumed run
    # continues the same sequence instead of reshuffling what is left.
    random.Random(args.seed).shuffle(cells)
    cells = [c for c in cells if (c["variant"], c["scenario"], c["model"], c["repeat"]) not in done]
    if args.limit > 0:
        cells = cells[: args.limit]
    est = len(cells) * 10 / max(1, args.parallel)
    print(
        f"{total} cells total, {len(done)} already in {args.out}, {len(cells)} to run "
        f"({len(variants)} variants x {len(scenarios)} scenarios x {len(args.models)} models "
        f"x {args.repeats} repeats); ~{est / 60:.1f} min at ~10 s/cell, parallel {args.parallel}",
        flush=True,
    )
    if args.dry_run:
        for c in cells:
            print(f"  {c['variant']} / {c['scenario']} / {c['model']} / {c['repeat']}")
        return 0
    if not cells:
        return 0
    if not args.no_marker and not supports_flag("--append-system-prompt"):
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
                        if row["error"]:
                            n_err += 1
                        if n_done % 10 == 0:
                            rate = (time.monotonic() - t0) / n_done
                            print(
                                f"[{n_done}/{len(cells)}] {n_err} errors, "
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

    print(f"done: {n_done} cells written to {args.out}, {n_err} with errors", flush=True)
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
