#!/usr/bin/env python3
"""The measuring half of the garden skill: what the prompt hook injected, how often the relevance
filter answered, and, once prompt-fact pairs are labeled, how much of what it injected was
relevant and which setting to move.

It reads the relevance filter's own logs in the memware home and writes only under
``<home>/labels/`` (which ``memware nuke`` removes). Standard library only: the memware CLI is
often installed in its own environment, so this never imports memware.

    garden.py health  [--days N]                 counts, rates and timings; no text
    garden.py sample  --work DIR [--days N]      blind labeling batches (these hold prompt text)
    garden.py disputes --work DIR                the pairs the first two passes disagreed on
    garden.py score   --work DIR [--record]      votes -> verdicts, then the report
    garden.py report  [--days N]                 the report over verdicts already on file
    garden.py clean   --work DIR                 delete a work directory

Nothing here prints a prompt or a fact. The batches it writes do hold them, owner-only, and
``clean`` removes them once the labels are in.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import random
import shutil
import sys
from pathlib import Path

T_GRID = [round(0.2 + 0.05 * i, 2) for i in range(9)]  # thresholds 0.20 … 0.60
K_GRID = list(range(3, 11))  # beliefs per prompt, 3 … 10
POOL_STEP, POOL_MIN, POOL_MAX = 5, 10, 50
MAX_LOSS_SHARE = 0.01  # the objective: keep at least 99% of what the current settings find
MIN_TUNE_PROMPTS, MIN_HOLDOUT_PROMPTS, MIN_RELEVANT = 100, 40, 50
EXPLORE_SHARE = 0.05  # relevant facts from the deepest logged ranks that justify a wider look
TIMEOUT_STEP, TIMEOUT_MAX = 0.5, 5.0
MAX_FALLBACK = 0.02
OFFENDER_MIN, OFFENDER_NOISE = 5, 0.8
BATCH_PAIRS = 600


def home() -> Path:
    """The same resolution as ``memware.config.memware_home``."""
    env = os.environ.get("MEMWARE_HOME")
    if env:
        return Path(env).expanduser()
    legacy = Path.home() / ".memware"
    if legacy.exists():
        return legacy
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "memware" if xdg else legacy


def garden_dir() -> Path:
    return home() / "labels" / "garden"


def _private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def _write_jsonl(path: Path, rows, mode: str = "w") -> None:
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn last line from a killed hook
    return out


def _ts(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _since(days: float | None) -> dt.datetime | None:
    return None if days is None else dt.datetime.now(dt.UTC) - dt.timedelta(days=days)


def _q(xs, f):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(f * len(xs)))] if xs else None


def config() -> dict:
    try:
        return json.loads((home() / "config.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def relevance_settings() -> dict:
    cfg = config()
    rel = cfg.get("relevance", {})
    k = cfg.get("inject", {}).get("k", 6)
    return {
        "k": k if isinstance(k, int) and not isinstance(k, bool) and 1 <= k <= 20 else 6,
        "mode": rel.get("mode", "off"),
        "threshold": rel.get("threshold", 0.5),
        "pool": rel.get("pool", 20),
        "timeout_s": rel.get("timeout_s", 1.5),
        "model": rel.get("model", "jev-1.13.0"),
    }


# --- the log -------------------------------------------------------------------------------


def log_rows(days: float | None) -> list[dict]:
    since = _since(days)
    rows = _read_jsonl(home() / "relevance-log.jsonl")
    return [r for r in rows if since is None or _ts(r["ts"]) >= since]


def prompts(rows: list[dict]) -> dict[str, list[dict]]:
    """One entry per prompt call. A prompt asked again keeps its latest call, which reflects the
    settings in force now."""
    calls: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for r in rows:
        calls[(r["prompt_id"], r["ts"])].append(r)
    latest: dict[str, list[dict]] = {}
    for (pid, _), rs in sorted(calls.items(), key=lambda kv: kv[0][1]):
        # Two calls for one prompt can share a timestamp (a hook and a retry in the same second):
        # keep one row per rank, or the prompt would look like a pool twice the size.
        by_rank = {}
        for r in rs:
            by_rank.setdefault(r["rank"], r)
        latest[pid] = [by_rank[k] for k in sorted(by_rank)]
    return latest


def group(r: dict) -> str:
    if r["chosen"] is None:
        return "unanswered"
    if r["today"]:
        return "kept" if r["chosen"] else "dropped"
    return "added" if r["chosen"] else "passed"


def pairs(rows: list[dict]) -> dict[str, dict]:
    """Each answered pair once, from its prompt's latest call."""
    out = {}
    for rs in prompts(rows).values():
        for r in rs:
            if r["chosen"] is not None:
                out[r["pair_id"]] = r
    return out


# --- health --------------------------------------------------------------------------------


def health(days: float | None) -> dict:
    rows = log_rows(days)
    P = prompts(rows)
    answered = {k: v for k, v in P.items() if v[0]["error"] is None and v[0]["p"] is not None}
    errors = collections.Counter(
        (v[0]["error"] or "no answer").split(":")[0][:40] for k, v in P.items() if k not in answered
    )
    g = collections.Counter(group(r) for rs in answered.values() for r in rs)
    emptied = sum(
        1
        for rs in answered.values()
        if any(r["today"] for r in rs) and not any(r["chosen"] for r in rs)
    )
    capped = sum(
        1
        for rs in answered.values()
        if sum(r["p"] >= r["threshold"] for r in rs) > sum(r["chosen"] for r in rs)
    )
    since = _since(days)
    usage = [
        u
        for u in _read_jsonl(home() / "relevance-usage.jsonl")
        if since is None or _ts(u["ts"]) >= since
    ]
    lat = [u["latency_ms"] for u in usage]
    labeled = {v["pair_id"] for v in _read_jsonl(garden_dir() / "verdicts.jsonl")}
    unlabeled = sum(
        1
        for rs in answered.values()
        if any(r["pair_id"] not in labeled for r in rs if r["today"] or r["chosen"])
    )
    n = len(answered) or 1
    # The window can span a settings change, so the timeout rule reads only the current mode's calls.
    mode = relevance_settings()["mode"]
    current = [v for v in P.values() if v[0]["mode"] == mode]
    return {
        "window_days": days,
        "settings": relevance_settings(),
        "prompts": len(P),
        "answered": len(answered),
        "fell_back": dict(errors),
        "fallback_rate": round(sum(errors.values()) / (len(P) or 1), 4),
        "prompts_current_mode": len(current),
        "timeouts_current_mode": sum(1 for v in current if "timeout" in (v[0]["error"] or "")),
        "calls": len(usage),
        "latency_ms_p50": _q(lat, 0.5),
        "latency_ms_p95": _q(lat, 0.95),
        "cost_usd": round(sum(u.get("cost_usd", 0) for u in usage), 4),
        "by_mode": dict(collections.Counter(v[0]["mode"] for v in answered.values())),
        "by_harness": dict(collections.Counter(v[0]["harness"] for v in answered.values())),
        "pairs": dict(g),
        "memware_alone_per_prompt": round((g["kept"] + g["dropped"]) / n, 2),
        "with_filter_per_prompt": round((g["kept"] + g["added"]) / n, 2),
        "emptied_blocks": emptied,
        "cap_bound_prompts": capped,
        "added_ranks": dict(
            sorted(
                collections.Counter(
                    r["rank"] for rs in answered.values() for r in rs if group(r) == "added"
                ).items()
            )
        ),
        "prompts_with_unlabeled_pairs": unlabeled,
        "last_cycle": (_read_jsonl(garden_dir() / "history.jsonl") or [{}])[-1].get("date"),
    }


# --- the replay grid -----------------------------------------------------------------------

# The log keeps the filter's score for every candidate in the pool, so any combination of
# threshold, k and pool (up to the pool that was logged) can be replayed on past prompts.


def pools_for(rs: list[dict]) -> list[int]:
    return list(range(POOL_MIN, len(rs) + 1, POOL_STEP)) or [len(rs)]


def pick(rs: list[dict], t: float, k: int, pool: int) -> list[dict]:
    """What the filter would inject: candidates ranked inside the pool that clear t, best first."""
    c = [r for r in rs if r["rank"] < pool and r["p"] is not None and r["p"] >= t - 1e-9]
    return sorted(c, key=lambda r: (-r["p"], r["rank"]))[:k]


def needed(rs: list[dict], cur: dict) -> dict[str, dict]:
    """Every pair some combination on the grid, or the current settings, would inject."""
    out = {}
    for pool in pools_for(rs):
        for r in pick(rs, T_GRID[0], K_GRID[-1], pool):
            out[r["pair_id"]] = r
    for r in pick(rs, cur["threshold"], cur["k"], min(cur["pool"], len(rs))):
        out[r["pair_id"]] = r
    return out


# --- sampling ------------------------------------------------------------------------------


def sample(
    work: Path, days: float | None, max_pairs: int, passed_per_prompt: int, seed: int
) -> dict:
    """Blind batches that complete prompts for the replay: for each prompt, every pair the grid
    could inject plus memware's own picks, and a few candidates below the grid, so misses show."""
    rng = random.Random(seed)
    cur = relevance_settings()
    labeled = {v["pair_id"] for v in _read_jsonl(garden_dir() / "verdicts.jsonl")}
    # A line past relevance.log_text_days keeps its scores but not its text: it can be replayed,
    # not labeled.
    P = [
        rs
        for rs in prompts(log_rows(days)).values()
        if rs[0]["p"] is not None and rs[0].get("prompt") and all(r.get("fact") for r in rs)
    ]
    rng.shuffle(P)
    chosen_prompts, total = [], 0
    for rs in P:
        want = needed(rs, cur) | {r["pair_id"]: r for r in rs if r["today"] or r["chosen"]}
        shown = [r for r in want.values() if r["pair_id"] not in labeled]
        below = [
            r
            for r in rs
            if r["pair_id"] not in want and r["pair_id"] not in labeled and r["p"] < T_GRID[0]
        ]
        if not shown:
            continue  # already complete; a prompt with only low candidates left adds nothing
        shown += rng.sample(below, min(passed_per_prompt, len(below)))
        if total + len(shown) > max_pairs and chosen_prompts:
            continue  # try a smaller prompt; the budget is a cap, not a stop
        chosen_prompts.append((rs[0]["prompt"], shown))
        total += len(shown)
    _private_dir(work)
    batches = []
    # Pack whole prompts into batches of at most BATCH_PAIRS pairs, so a labeler sees each prompt once.
    cur_pack: list = []
    n = 0
    packs = []
    for prompt, shown in chosen_prompts:
        if cur_pack and n + len(shown) > BATCH_PAIRS:
            packs.append(cur_pack)
            cur_pack, n = [], 0
        cur_pack.append((prompt, shown))
        n += len(shown)
    if cur_pack:
        packs.append(cur_pack)
    key = []
    for i, pack in enumerate(packs, 1):
        out_lines = []
        for prompt, shown in pack:
            facts = [{"pair_id": r["pair_id"], "fact": r["fact"]} for r in shown]
            rng.shuffle(facts)
            out_lines.append({"prompt": prompt, "facts": facts})
            key += [
                {"pair_id": r["pair_id"], "group": group(r), "p": r["p"], "rank": r["rank"]}
                for r in shown
            ]
        rng.shuffle(out_lines)
        name = f"batch{i}.jsonl"
        _write_jsonl(work / name, out_lines)
        batches.append(
            {"file": str(work / name), "prompts": len(pack), "pairs": sum(len(s) for _, s in pack)}
        )
    _private_dir(work / "labels")
    _write_jsonl(work / "key.jsonl", key)  # groups and scores only; labelers must not read it
    return {
        "batches": batches,
        "prompts": len(chosen_prompts),
        "pairs": total,
        "groups": dict(collections.Counter(k["group"] for k in key)),
        "already_labeled_skipped": len(labeled),
    }


# --- votes and verdicts --------------------------------------------------------------------


def _votes(work: Path) -> dict[str, list[bool]]:
    votes: dict[str, list[bool]] = collections.defaultdict(list)
    for f in sorted((work / "labels").glob("*.jsonl")):
        for r in _read_jsonl(f):
            if isinstance(r.get("relevant"), bool):
                votes[r["pair_id"]].append(r["relevant"])
    return votes


def verdict(v: list[bool]) -> str:
    t = sum(v)
    return "relevant" if t * 2 > len(v) else "noise" if t * 2 < len(v) else "split"


def check(work: Path) -> list[str]:
    """Each label file must cover exactly one batch (or the disputes file), each pair once."""
    problems = []
    want = {}
    for b in sorted(work.glob("batch*.jsonl")):
        want[b.stem] = {f["pair_id"] for line in _read_jsonl(b) for f in line["facts"]}
    for f in sorted((work / "labels").glob("*.jsonl")):
        stem = f.stem.rsplit("-", 1)[0]
        ids = [r.get("pair_id") for r in _read_jsonl(f)]
        if stem not in want:
            problems.append(f"{f.name}: no batch named {stem}")
        elif len(ids) != len(set(ids)) or set(ids) != want[stem]:
            problems.append(
                f"{f.name}: covers {len(set(ids))} of {len(want[stem])} pairs, {len(ids) - len(set(ids))} repeated"
            )
    return problems


def disputes(work: Path) -> dict:
    votes = _votes(work)
    split = {k for k, v in votes.items() if len(v) >= 2 and len(set(v)) > 1}
    lines = []
    for b in sorted(work.glob("batch[0-9]*.jsonl")):
        for line in _read_jsonl(b):
            facts = [f for f in line["facts"] if f["pair_id"] in split]
            if facts:
                lines.append({"prompt": line["prompt"], "facts": facts})
    if lines:
        _write_jsonl(work / "batchdisputes.jsonl", lines)
    return {
        "disputed_pairs": len(split),
        "file": str(work / "batchdisputes.jsonl") if lines else None,
    }


def consolidate(work: Path) -> int:
    votes = _votes(work)
    date = dt.date.today().isoformat()
    have = {v["pair_id"] for v in _read_jsonl(garden_dir() / "verdicts.jsonl")}
    new = [
        {"pair_id": k, "verdict": verdict(v), "votes": v, "cycle": date}
        for k, v in votes.items()
        if k not in have
    ]
    _private_dir(garden_dir())
    _write_jsonl(garden_dir() / "verdicts.jsonl", new, mode="a")
    return len(new)


# --- the report ----------------------------------------------------------------------------


def _prec(c: collections.Counter) -> float | None:
    n = c["relevant"] + c["noise"]
    return round(c["relevant"] / n, 3) if n else None


def _combo(P: list[list[dict]], V: dict, t: float, k: int, pool: int) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for rs in P:
        for r in pick(rs, t, k, min(pool, len(rs))):
            c[V.get(r["pair_id"], "unlabeled")] += 1
    return c


def _row(t, k, pool, c) -> dict:
    return {
        "threshold": t,
        "k": k,
        "pool": pool,
        "relevant": c["relevant"],
        "noise": c["noise"],
        "precision": _prec(c),
    }


def _toward(cur: float, opt: float, step: float) -> float:
    """Halfway from cur to opt, in whole steps, and at least one step when they differ."""
    if abs(opt - cur) < 1e-9:
        return cur
    n = max(1, round(abs(opt - cur) / 2 / step))
    n = min(n, round(abs(opt - cur) / step))
    return round(cur + math.copysign(n * step, opt - cur), 2)


def replay(days: float | None, V: dict, V_cycle: dict, cur: dict) -> dict:
    """The joint search. Objective: the least noise among settings that keep at least 99% of the
    relevant facts the current settings inject. It is chosen on older prompts and must hold on
    the newest cycle's before anything moves."""
    P = [
        rs
        for rs in prompts(log_rows(days)).values()
        if rs[0]["p"] is not None and all(p in V for p in needed(rs, cur))
    ]
    cycles = sorted({max(V_cycle[p] for p in needed(rs, cur)) for rs in P if needed(rs, cur)})
    latest = cycles[-1] if cycles else None
    hold = [
        rs for rs in P if needed(rs, cur) and max(V_cycle[p] for p in needed(rs, cur)) == latest
    ]
    tune = [rs for rs in P if rs not in hold]
    split = "by cycle"
    if len(tune) < MIN_TUNE_PROMPTS or len(hold) < MIN_HOLDOUT_PROMPTS:
        # First cycles: hold out a fixed third of the prompts instead.
        hold = [rs for rs in P if int(rs[0]["prompt_id"][:8], 16) % 3 == 0]
        tune = [rs for rs in P if int(rs[0]["prompt_id"][:8], 16) % 3 != 0]
        split = "by prompt hash"
    out: dict = {
        "complete_prompts": len(P),
        "tune": len(tune),
        "holdout": len(hold),
        "split": split,
    }
    t0, k0, pool0 = cur["threshold"], cur["k"], cur["pool"]
    base = _combo(tune, V, t0, k0, pool0)
    out["current"] = _row(t0, k0, pool0, base)
    if len(tune) < MIN_TUNE_PROMPTS or base["relevant"] < MIN_RELEVANT:
        out["verdict"] = (
            f"not enough complete prompts: {len(tune)} to tune on ({MIN_TUNE_PROMPTS} needed), "
            f"{base['relevant']} relevant injected ({MIN_RELEVANT} needed)"
        )
        return out
    floor = (1 - MAX_LOSS_SHARE) * base["relevant"]
    max_pool = max(len(rs) for rs in P)
    feasible = []
    for t in T_GRID:
        for k in K_GRID:
            for pool in range(POOL_MIN, max_pool + 1, POOL_STEP):
                c = _combo(tune, V, t, k, pool)
                if c["relevant"] >= floor:
                    dist = abs(t - t0) / 0.05 + abs(k - k0) + abs(pool - pool0) / POOL_STEP
                    feasible.append(((c["noise"], -c["relevant"], dist), t, k, pool, c))
    feasible.sort(key=lambda f: f[0])
    out["top"] = [_row(t, k, pool, c) for _, t, k, pool, c in feasible[:5]]
    _, t1, k1, pool1, c1 = feasible[0]
    out["optimum"] = _row(t1, k1, pool1, c1)

    def better(a: collections.Counter, b: collections.Counter) -> bool:
        """The objective's order: less noise, then more relevant facts."""
        return (a["noise"], -a["relevant"]) < (b["noise"], -b["relevant"])

    def holds(t, k, pool) -> tuple[bool, dict]:
        h0, h1 = _combo(hold, V, t0, k0, pool0), _combo(hold, V, t, k, pool)
        lost = h0["relevant"] - h1["relevant"]
        ok = lost <= max(1, int(MAX_LOSS_SHARE * h0["relevant"])) and better(h1, h0)
        return ok, {"current": _row(t0, k0, pool0, h0), "candidate": _row(t, k, pool, h1)}

    # At the edge of what was logged, the replay cannot see further: say so, and suggest a look.
    if pool1 == max_pool and pool1 + 2 * POOL_STEP <= POOL_MAX:
        deep = sum(
            1
            for rs in tune
            for r in pick(rs, t1, k1, pool1)
            if r["rank"] >= pool1 - POOL_STEP and V[r["pair_id"]] == "relevant"
        )
        if c1["relevant"] and deep / c1["relevant"] >= EXPLORE_SHARE:
            out["explore_pool"] = {
                "to": pool1 + 2 * POOL_STEP,
                "why": f"{deep} of {c1['relevant']} relevant facts at the optimum came from ranks "
                f"{pool1 - POOL_STEP}+, the deepest logged; a wider pool for one cycle lets the "
                "next replay see past it. It sends about half again as many tokens, and the "
                "filter's scores may shift with more candidates beside them.",
            }

    ok, check = holds(t1, k1, pool1)
    out["holdout_check"] = check
    if (t1, k1, pool1) == (t0, k0, pool0):
        out["verdict"] = "the current settings are already the best on the grid"
        return out
    # Even when the optimum fails the held-out prompts, a smaller step toward it may not: every
    # candidate below must hold there before it is proposed.

    # Damping: the best setting inside the box from the current one to halfway, on every axis. The
    # halfway corner itself can be worse than both ends (a higher k before the higher threshold
    # has cut), so the box is searched rather than its corner taken.
    half = (_toward(t0, t1, 0.05), _toward(k0, k1, 1), _toward(pool0, pool1, POOL_STEP))

    def between(a: float, b: float, step: float) -> list[float]:
        lo, hi = min(a, b), max(a, b)
        return [round(lo + i * step, 2) for i in range(round((hi - lo) / step) + 1)]

    inside = []
    for t in between(t0, half[0], 0.05):
        for k in between(k0, half[1], 1):
            for pool in between(pool0, half[2], POOL_STEP):
                c = _combo(tune, V, t, int(k), int(pool))
                if c["relevant"] >= floor and better(c, base):
                    inside.append(((c["noise"], -c["relevant"]), (t, int(k), int(pool)), c))
    inside.sort(key=lambda x: x[0])
    # Nothing inside the box improves: take the nearest improving setting instead, so a move is
    # still as small as it can be. The optimum itself is the last candidate.
    nearest = sorted(
        (f for f in feasible if better(f[4], base)),
        key=lambda f: (f[0][2], f[0][0], f[0][1]),
    )
    step = None
    for combo, c in [(combo, c) for _, combo, c in inside] + [
        ((t, k, pool), c) for _, t, k, pool, c in nearest
    ]:
        if holds(*combo)[0]:
            step, c_step = combo, c
            break
    if step is None:
        out["verdict"] = (
            "no setting that improves on the tuning prompts held on the held-out ones; "
            "nothing moves"
        )
        return out
    out["move"] = _row(*step, c_step)
    out["move_holdout"] = holds(*step)[1]["candidate"]
    if step == (t1, k1, pool1):
        out["verdict"] = "move to the optimum"
    elif ok:
        out["verdict"] = "move part of the way to the optimum"
    else:
        out["verdict"] = "the optimum did not hold on the held-out prompts; move the part that did"
    return out


def report(days: float | None) -> dict:
    verdicts = _read_jsonl(garden_dir() / "verdicts.jsonl")
    V = {v["pair_id"]: v["verdict"] for v in verdicts}
    V_cycle = {v["pair_id"]: v.get("cycle", "") for v in verdicts}
    rows = log_rows(days)
    Pa = {k: r for k, r in pairs(rows).items() if k in V}
    rel = relevance_settings()
    by = collections.defaultdict(collections.Counter)
    for k, r in Pa.items():
        by[group(r)][V[k]] += 1
    alone = by["kept"] + by["dropped"]
    filt = by["kept"] + by["added"]
    out: dict = {
        "labeled_pairs_in_window": len(Pa),
        "groups": {g: dict(c) | {"precision": _prec(c)} for g, c in by.items()},
        "memware_alone": dict(alone) | {"precision": _prec(alone)},
        "with_filter": dict(filt) | {"precision": _prec(filt)},
        "misses": dict(by["passed"]),
    }
    out["replay"] = replay(days, V, V_cycle, rel) if rel["mode"] != "off" else {}

    # Beliefs the hook keeps injecting as noise.
    inj = collections.defaultdict(collections.Counter)
    for k, r in Pa.items():
        if r["chosen"] if r["mode"] == "filter" else r["today"]:  # what actually reached the prompt
            inj[r["belief_id"]][V[k]] += 1
    rule = collections.defaultdict(collections.Counter)  # memware's own picks, filter or not
    for k, r in Pa.items():
        if r["today"]:
            rule[r["belief_id"]][V[k]] += 1

    # How often the filter itself chose each belief: an offender it already drops matters less now.
    by_filter = collections.Counter(
        r["belief_id"] for r in Pa.values() if r["mode"] == "filter" and r["chosen"]
    )

    def offenders(src):
        o = []
        for b, c in src.items():
            n = sum(c.values())
            if n >= OFFENDER_MIN and c["noise"] / n >= OFFENDER_NOISE:
                o.append(
                    {
                        "belief_id": b,
                        "shown": n,
                        "noise": c["noise"],
                        "relevant": c["relevant"],
                        "by_filter": by_filter[b],
                    }
                )
        return sorted(o, key=lambda x: (-x["by_filter"], -x["noise"]))

    out["offenders_injected"] = offenders(inj)
    out["offenders_subject_rule"] = offenders(rule)[:25]
    out["proposals"] = propose(out, rel, health(days))
    return out


def propose(rep: dict, rel: dict, h: dict) -> list[dict]:
    """The decision rules. Each proposal names the setting, the value, and the evidence."""
    props: list[dict] = []
    rp = rep["replay"]
    if rel["mode"] == "filter" and "move" in rp:
        mv, cur = rp["move"], rp["current"]
        why = (
            f"on {rp['tune']} prompts: {cur['relevant']} -> {mv['relevant']} relevant, "
            f"{cur['noise']} -> {mv['noise']} noise; held on {rp['holdout']} held-out prompts"
        )
        for name, key in (
            ("relevance.threshold", "threshold"),
            ("inject.k", "k"),
            ("relevance.pool", "pool"),
        ):
            if mv[key] != cur[key]:
                props.append({"set": name, "from": cur[key], "to": mv[key], "why": why})
    # Widen the pool only once the rest has settled: the filter's scores can shift with more
    # candidates, and a move made in the same cycle could not be told apart from it.
    if (
        rel["mode"] == "filter"
        and "explore_pool" in rp
        and not props
        and "already the best" in rp.get("verdict", "")
    ):
        props.append(
            {"set": "relevance.pool", "from": rel["pool"], "to": rp["explore_pool"]["to"]}
            | {"why": rp["explore_pool"]["why"], "explore": True}
        )
    if rel["mode"] == "filter" and not props and rp.get("verdict"):
        props.append({"note": rp["verdict"]})

    timeouts, n_cur = h["timeouts_current_mode"], h["prompts_current_mode"]
    if (
        n_cur >= 100
        and timeouts / n_cur > MAX_FALLBACK
        and rel["timeout_s"] + TIMEOUT_STEP <= TIMEOUT_MAX
    ):
        props.append(
            {
                "set": "relevance.timeout_s",
                "from": rel["timeout_s"],
                "to": rel["timeout_s"] + TIMEOUT_STEP,
                "why": f"{timeouts} of {n_cur} prompts in {rel['mode']} mode timed out and fell back",
            }
        )

    a, f = rep["memware_alone"], rep["with_filter"]
    if (
        rel["mode"] == "filter"
        and a.get("precision") is not None
        and f.get("precision") is not None
        and f["precision"] - a["precision"] < 0.05
        and f.get("relevant", 0) <= a.get("relevant", 0)
    ):
        props.append(
            {
                "set": "relevance.mode",
                "from": "filter",
                "to": "shadow",
                "why": "the filter no longer beats memware's own picks by 5 points and finds no more relevant facts",
            }
        )
    return props


def record(rep: dict, h: dict, new_verdicts: int) -> None:
    line = {
        "date": dt.date.today().isoformat(),
        "window_days": h["window_days"],
        "settings": h["settings"],
        "prompts": h["prompts"],
        "fallback_rate": h["fallback_rate"],
        "latency_ms_p50": h["latency_ms_p50"],
        "latency_ms_p95": h["latency_ms_p95"],
        "cost_usd": h["cost_usd"],
        "new_verdicts": new_verdicts,
        "labeled_pairs_in_window": rep["labeled_pairs_in_window"],
        "memware_alone": rep["memware_alone"],
        "with_filter": rep["with_filter"],
        "misses": rep["misses"],
        "replay": {k: v for k, v in rep["replay"].items() if k != "top"},
        "offenders": [o["belief_id"] for o in rep["offenders_injected"]],
        "proposals": rep["proposals"],
    }
    _private_dir(garden_dir())
    _write_jsonl(garden_dir() / "history.jsonl", [line], mode="a")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("health", "report"):
        s = sub.add_parser(name)
        s.add_argument("--days", type=float, default=None)
    s = sub.add_parser("sample")
    s.add_argument("--work", type=Path, required=True)
    s.add_argument("--days", type=float, default=None)
    s.add_argument("--max-pairs", type=int, default=600)
    s.add_argument("--passed-per-prompt", type=int, default=1)
    s.add_argument("--seed", type=int, default=7)
    for name in ("disputes", "clean"):
        sub.add_parser(name).add_argument("--work", type=Path, required=True)
    s = sub.add_parser("score")
    s.add_argument("--work", type=Path, required=True)
    s.add_argument("--days", type=float, default=None)
    s.add_argument("--record", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "health":
        res = health(a.days)
    elif a.cmd == "sample":
        res = sample(a.work, a.days, a.max_pairs, a.passed_per_prompt, a.seed)
    elif a.cmd == "disputes":
        res = disputes(a.work)
    elif a.cmd == "score":
        problems = check(a.work)
        if problems:
            print(json.dumps({"label_file_problems": problems}, indent=2))
            return 1
        n = consolidate(a.work)
        res = report(a.days)
        res["new_verdicts"] = n
        if a.record:
            record(res, health(a.days), n)
    elif a.cmd == "report":
        res = report(a.days)
    else:
        if (a.work / "key.jsonl").exists():  # only ever a directory `sample` made
            shutil.rmtree(a.work)
        res = {"removed": str(a.work)}
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
