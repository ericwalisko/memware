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
import sys
import time
from pathlib import Path
from typing import Any

from memware.index import search_beliefs, search_turns
from memware.ingest import sync_tree
from memware.store import DEFAULT_DB, Store

MARKER = "[memware-eval]"
"""Put this in every prompt an evaluation sends. Runs that carry it are excluded when
``--corpus`` rebuilds a clean store, and ``MEMWARE_NO_CAPTURE=1`` in the run's environment
keeps hooks from indexing them in the first place."""


def build_clean_store(
    db: str,
    corpus: Path,
    *,
    harness: str = "claude-code",
    beliefs_from: str | None = None,
    exclude: list[str] | None = None,
    also_skip: list[str] | None = None,
) -> dict[str, int]:
    """Index ``corpus`` into a fresh store at ``db``, skipping files that contain MARKER
    (or any of ``also_skip`` — e.g. the prompt text of an older evaluation harness),
    and copy the belief ledger from ``beliefs_from`` so retrieval is judged against the
    same beliefs the live system has."""
    for suffix in ("", "-wal", "-shm"):
        Path(db + suffix).unlink(missing_ok=True)
    with Store(db) as s:
        rep = sync_tree(
            s,
            corpus,
            harness=harness,
            skip_if_contains=[MARKER, *(also_skip or [])],
            exclude=exclude,
        )
        copied = 0
        if beliefs_from:
            s.conn.execute("ATTACH ? AS live", (str(Path(beliefs_from).expanduser()),))
            s.conn.execute("INSERT INTO belief SELECT * FROM live.belief")
            copied = int(s.conn.execute("SELECT count(*) FROM belief").fetchone()[0])
            s.conn.execute("DETACH live")
        return {"files": len(rep), "turns": sum(rep.values()), "beliefs": copied}


def retrieve(store: Store, question: str, k: int) -> tuple[str, str]:
    """Return (beliefs_context, beliefs_plus_turns_context).

    The beliefs context mirrors what the prompt-time hook injects, so it uses the
    same subject gate (``require_subject=True``); the turns context mirrors an
    explicit, broad recall.
    """
    beliefs = search_beliefs(store, question, k=k, record_use=False, require_subject=True)
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
                    "beliefs_injected": bool(b_ctx),
                    "beliefs": _score(q, b_ctx),
                    "beliefs+turns": _score(q, bt_ctx),
                }
            )

    def summarize(ctx_key: str) -> dict[str, Any]:
        by_type: dict[str, list[bool]] = {}
        for r in results:
            by_type.setdefault(str(r["type"]), []).append(bool(r[ctx_key]["ok"]))
        n = max(1, len(results))
        return {
            "accuracy": sum(bool(r[ctx_key]["ok"]) for r in results) / n,
            "stale_rate": sum(bool(r[ctx_key]["stale"]) for r in results) / n,
            "by_type": {t: sum(v) / len(v) for t, v in by_type.items()},
        }

    n_all = max(1, len(results))
    return {
        "n": len(results),
        "beliefs_injection_rate": sum(bool(r["beliefs_injected"]) for r in results) / n_all,
        "beliefs_injection_rate_negatives": (
            sum(bool(r["beliefs_injected"]) for r in results if r["type"] == "negative")
            / max(1, sum(1 for r in results if r["type"] == "negative"))
        ),
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
    p.add_argument(
        "--corpus",
        type=Path,
        help="rebuild --db as a clean scratch store from this transcript root first "
        f"(files containing {MARKER!r} are skipped)",
    )
    p.add_argument(
        "--beliefs-from", help="copy the belief ledger from this store into the scratch store"
    )
    p.add_argument("--harness", default="claude-code")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    p.add_argument(
        "--also-skip",
        action="append",
        default=[],
        metavar="TEXT",
        help="additional content markers that identify evaluation transcripts (repeatable)",
    )
    a = p.parse_args(argv)
    if a.corpus:
        if str(Path(a.db).expanduser()) == str(DEFAULT_DB):
            raise SystemExit(
                "--corpus rebuilds the store: pass a scratch --db, not the default one"
            )
        built = build_clean_store(
            a.db, a.corpus, harness=a.harness, beliefs_from=a.beliefs_from, exclude=a.exclude
        )
        print(f"clean store: {built}", file=sys.stderr)
    rep = run(a.db, a.questions, k=a.k)
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        for key in ("beliefs", "beliefs+turns"):
            sm = rep[key]
            print(
                f"[{key}] n={rep['n']} accuracy={sm['accuracy']:.3f} "
                f"stale_rate={sm['stale_rate']:.3f} by_type={sm['by_type']}"
            )
        print(
            f"beliefs injected on {rep['beliefs_injection_rate']:.0%} of questions "
            f"({rep['beliefs_injection_rate_negatives']:.0%} of negatives); "
            f"median_latency={rep['latency_ms_median']:.1f}ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
