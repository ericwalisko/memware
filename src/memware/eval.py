"""Retrieval-level evaluation: does recall surface the right evidence, and never the stale value?

A question set is JSONL, one object per line::

    {"id": "q1", "question": "which port does the api listen on",
     "expect_any": ["8443"], "not_expect": ["8080"], "type": "stale"}

``type`` is ``fact`` (answer exists), ``stale`` (answer changed over time; the
old value must not appear) or ``negative`` (nothing relevant should be found).
Scores are containment against the retrieved context, so no model is needed
and results are reproducible. End-to-end runs with a model are described in
docs/eval.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from memware.index import search_beliefs, search_turns
from memware.store import DEFAULT_DB, Store


def retrieve(store: Store, question: str, k: int) -> str:
    hits = search_beliefs(store, question, k=k, record_use=False) + search_turns(
        store, question, k=k, record_use=False
    )
    return "\n".join(h.text for h in hits)


def run(db: str, questions: Path, *, k: int = 8) -> dict[str, object]:
    rows = [
        json.loads(line)
        for line in questions.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results: list[dict[str, object]] = []
    latencies: list[float] = []
    with Store(db) as s:
        for q in rows:
            t0 = time.perf_counter()
            ctx = retrieve(s, q["question"], k).lower()
            latencies.append((time.perf_counter() - t0) * 1000)
            found = any(e.lower() in ctx for e in q.get("expect_any", []))
            stale = any(n.lower() in ctx for n in q.get("not_expect", []))
            qtype = q.get("type", "fact")
            ok = (not ctx) if qtype == "negative" else (found and not stale)
            results.append(
                {
                    "id": q["id"],
                    "type": qtype,
                    "found": found,
                    "stale": stale,
                    "ok": ok,
                    "context_chars": len(ctx),
                }
            )
    by_type: dict[str, list[bool]] = {}
    for r in results:
        by_type.setdefault(str(r["type"]), []).append(bool(r["ok"]))
    n = max(1, len(results))
    return {
        "n": len(results),
        "accuracy": sum(bool(r["ok"]) for r in results) / n,
        "stale_rate": sum(bool(r["stale"]) for r in results) / n,
        "by_type": {t: sum(v) / len(v) for t, v in by_type.items()},
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
        print(
            f"n={rep['n']} accuracy={rep['accuracy']:.3f} stale_rate={rep['stale_rate']:.3f} "
            f"median_latency={rep['latency_ms_median']:.1f}ms by_type={rep['by_type']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
