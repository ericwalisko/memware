"""Retrieval-level evaluation: does recall surface the right evidence, and never the stale value?

A question set is JSONL, one object per line::

    {"id": "q1", "question": "which port does the api listen on",
     "expect_any": ["8443"], "not_expect": ["8080"], "type": "stale"}

``type`` is ``fact`` (answer exists), ``stale`` (answer changed over time; the
old value must not appear) or ``negative`` (nothing relevant should be found).
Scores are containment against the retrieved context, so no model is needed
and results are reproducible. Two contexts are scored for every question:
``beliefs`` (currently valid beliefs only — what a prompt-time hook injects)
and ``beliefs+turns`` (beliefs plus transcript evidence). Transcripts are
evidence and legitimately contain old values, so a stale value appearing in
``beliefs+turns`` is expected; appearing in ``beliefs`` is a defect. End-to-end
runs with a model are described in docs/eval.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from memware.index import search_beliefs, search_turns
from memware.store import DEFAULT_DB, Store


def retrieve(store: Store, question: str, k: int) -> tuple[str, str]:
    """Return (beliefs_context, beliefs_plus_turns_context)."""
    beliefs = search_beliefs(store, question, k=k, record_use=False)
    turns = search_turns(store, question, k=k, record_use=False)
    b = "\n".join(h.text for h in beliefs)
    return b, "\n".join([b, *(h.text for h in turns)]).strip()


def _first(ctx: str, needles: list[Any]) -> int:
    pos = [ctx.find(str(n).lower()) for n in needles]
    pos = [p for p in pos if p >= 0]
    return min(pos) if pos else -1


def _score(q: dict[str, Any], ctx: str) -> dict[str, Any]:
    """``stale`` means the old value leads: it appears and the expected value does not
    appear before it. Mentioning the old value as history after the current one is fine."""
    ctx = ctx.lower()
    found = any(str(e).lower() in ctx for e in q.get("expect_any", []))
    old_at = _first(ctx, q.get("not_expect", []))
    new_at = _first(ctx, q.get("expect_any", []))
    stale = old_at >= 0 and (new_at < 0 or old_at < new_at)
    qtype = str(q.get("type", "fact"))
    ok = (not found) if qtype == "negative" else (found and not stale)
    return {"found": found, "stale": stale, "ok": ok, "context_chars": len(ctx)}


def run(db: str, questions: Path, *, k: int = 8) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in questions.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results: list[dict[str, Any]] = []
    latencies: list[float] = []
    with Store(db) as s:
        for q in rows:
            t0 = time.perf_counter()
            b_ctx, bt_ctx = retrieve(s, q["question"], k)
            latencies.append((time.perf_counter() - t0) * 1000)
            results.append(
                {
                    "id": q["id"],
                    "type": q.get("type", "fact"),
                    "beliefs": _score(q, b_ctx),
                    "beliefs+turns": _score(q, bt_ctx),
                }
            )

    def summarize(ctx_key: str) -> dict[str, Any]:
        by_type: dict[str, list[bool]] = {}
        for r in results:
            by_type.setdefault(str(r["type"]), []).append(bool(r[ctx_key]["ok"]))
        n = max(1, len(results))
        chars = [int(r[ctx_key]["context_chars"]) for r in results]
        return {
            "accuracy": sum(bool(r[ctx_key]["ok"]) for r in results) / n,
            "stale_rate": sum(bool(r[ctx_key]["stale"]) for r in results) / n,
            "by_type": {t: sum(v) / len(v) for t, v in by_type.items()},
            "context_chars_median": statistics.median(chars) if chars else 0,
        }

    return {
        "n": len(results),
        "beliefs": summarize("beliefs"),
        "beliefs+turns": summarize("beliefs+turns"),
        "latency_ms_median": statistics.median(latencies) if latencies else 0.0,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="memware-eval", description=__doc__)
    p.add_argument("questions", type=Path)
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    rep = run(a.db, a.questions, k=a.k)
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        for key in ("beliefs", "beliefs+turns"):
            sm = rep[key]
            print(
                f"[{key}] n={rep['n']} accuracy={sm['accuracy']:.3f} "
                f"stale_rate={sm['stale_rate']:.3f} by_type={sm['by_type']} "
                f"context_chars={sm['context_chars_median']:.0f}"
            )
        print(f"median_latency={rep['latency_ms_median']:.1f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
