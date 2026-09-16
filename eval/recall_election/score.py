"""Score recall-election results.

Reads the JSONL run.py writes and reports, per (variant, model): election rate on positives
(TPR), false-election rate on negatives (FPR), balanced accuracy, Wilson 95% intervals, mean
phrasings when recall was called, and how often recall was the first tool on positives. Then,
per scenario across variants, the election rate, flagging scenarios with no signal (0 or 1
everywhere). Invalid cells (timeouts, non-zero exit, is_error) are excluded and counted.

    uv run python eval/recall_election/score.py results.jsonl   # writes report.md beside it
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

RECALL = "mcp__memware__recall"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials (nan, nan when n == 0)."""
    if n <= 0:
        return (math.nan, math.nan)
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(k: int, n: int) -> float:
    return k / n if n else math.nan


def fmt(x: float, digits: int = 2) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def fmt_ci(k: int, n: int) -> str:
    if n == 0:
        return "-"
    lo, hi = wilson(k, n)
    return f"{k / n:.2f} [{lo:.2f}, {hi:.2f}]"


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def is_valid(row: dict[str, Any]) -> bool:
    if "valid" in row:
        return bool(row["valid"])
    return not row.get("error")


def summarise_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pos = [r for r in rows if r.get("expect")]
    neg = [r for r in rows if not r.get("expect")]
    tp = sum(1 for r in pos if r.get("recall_called"))
    fp = sum(1 for r in neg if r.get("recall_called"))
    tpr, fpr = rate(tp, len(pos)), rate(fp, len(neg))
    bal = math.nan if math.isnan(tpr) or math.isnan(fpr) else (tpr + (1 - fpr)) / 2
    phr = [int(r.get("n_phrasings") or 0) for r in rows if r.get("recall_called")]
    first = sum(1 for r in pos if r.get("first_tool") == RECALL)
    return {
        "n": len(rows),
        "n_pos": len(pos),
        "n_neg": len(neg),
        "tp": tp,
        "fp": fp,
        "tpr": tpr,
        "fpr": fpr,
        "bal_acc": bal,
        "mean_phrasings": (sum(phr) / len(phr)) if phr else math.nan,
        "n_recall": len(phr),
        "first_recall": first,
        "first_recall_rate": rate(first, len(pos)),
        "mismatch": sum(1 for r in rows if r.get("recall_mismatch")),
    }


def table(header: list[str], body: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(cells) + " |" for cells in body]
    return "\n".join(lines)


CANDIDATE = "synthesized"
CONTROL = "control"
ALPHA = 0.05


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact sign test p-value; ties are excluded before calling (1.0 when n == 0)."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def paired_cells(
    rows: list[dict[str, Any]], model: str, a: str, b: str
) -> list[tuple[bool, bool, str]]:
    """Matched (scenario, repeat) cells of variant ``a`` vs ``b``: (a_correct, b_correct, id)."""

    def index(variant: str) -> dict[tuple[str, int], dict[str, Any]]:
        return {
            (str(r.get("scenario")), int(r.get("repeat", 0))): r
            for r in rows
            if str(r.get("variant")) == variant and str(r.get("model")) == model
        }

    ia, ib = index(a), index(b)
    out = []
    for key in sorted(ia.keys() & ib.keys()):
        ra, rb = ia[key], ib[key]
        expect = bool(ra.get("expect"))
        out.append(
            (
                bool(ra.get("recall_called")) == expect,
                bool(rb.get("recall_called")) == expect,
                f"{key[0]}#{key[1]}",
            )
        )
    return out


def tpr(rows: list[dict[str, Any]], variant: str, model: str) -> tuple[int, int]:
    pos = [
        r
        for r in rows
        if str(r.get("variant")) == variant and str(r.get("model")) == model and r.get("expect")
    ]
    return sum(1 for r in pos if r.get("recall_called")), len(pos)


def decision_section(valid: list[dict[str, Any]]) -> list[str]:
    """The pre-registered rule: ship the candidate only if it beats control on BOTH models.

    Per model, on matched (scenario, repeat) cells: a paired two-sided sign test on which
    variant got the cell right, and the candidate's positive election rate must not be lower
    than control's. Ship requires p < 0.05 with the candidate ahead, and no TPR regression,
    on EVERY model separately. Anything else keeps control.
    """
    models = sorted({str(r.get("model")) for r in valid})
    present = {str(r.get("variant")) for r in valid}
    out = [
        "",
        "## Decision rule",
        "",
        f"Ship `{CANDIDATE}` only if it beats `{CONTROL}` at p < {ALPHA} on each model "
        "separately (paired sign test over matched (scenario, repeat) cells, correctness = "
        "recall elected iff expected) AND its positive election rate is not lower. "
        "Otherwise keep `{0}`.".format(CONTROL),
        "",
    ]
    if not {CANDIDATE, CONTROL} <= present:
        missing = sorted({CANDIDATE, CONTROL} - present)
        out += [f"**VERDICT: keep `{CONTROL}`** — no result for {', '.join(missing)}.", ""]
        return out

    body, verdicts = [], []
    for m in models:
        pairs = paired_cells(valid, m, CANDIDATE, CONTROL)
        wins = sum(1 for a, b, _ in pairs if a and not b)
        losses = sum(1 for a, b, _ in pairs if b and not a)
        ties = len(pairs) - wins - losses
        p = sign_test(wins, losses)
        ck, cn = tpr(valid, CANDIDATE, m)
        bk, bn = tpr(valid, CONTROL, m)
        tpr_ok = cn > 0 and bn > 0 and (ck / cn) >= (bk / bn)
        ok = p < ALPHA and wins > losses and tpr_ok
        verdicts.append(ok)
        body.append(
            [
                m,
                str(len(pairs)),
                str(wins),
                str(losses),
                str(ties),
                f"{p:.4f}",
                f"{fmt(rate(ck, cn))} vs {fmt(rate(bk, bn))}",
                "yes" if tpr_ok else "NO",
                "pass" if ok else "fail",
            ]
        )
    out.append(
        table(
            [
                "model",
                "paired cells",
                f"{CANDIDATE} wins",
                f"{CONTROL} wins",
                "ties",
                "sign-test p",
                f"pos. rate {CANDIDATE} vs {CONTROL}",
                "no TPR loss",
                "rule",
            ],
            body,
        )
    )
    ship = bool(verdicts) and all(verdicts)
    out += [
        "",
        f"**VERDICT: {'SHIP `' + CANDIDATE + '`' if ship else 'KEEP `' + CONTROL + '`'}** "
        f"({sum(verdicts)}/{len(verdicts)} models pass the rule).",
        "",
    ]
    return out


def build_report(rows: list[dict[str, Any]], source: Path) -> str:
    valid = [r for r in rows if is_valid(r)]
    invalid = [r for r in rows if not is_valid(r)]
    variants = sorted({str(r.get("variant")) for r in valid})
    models = sorted({str(r.get("model")) for r in valid})

    out: list[str] = ["# Recall election report", "", f"Source: `{source}`", ""]
    out.append(
        f"{len(rows)} cells, {len(valid)} valid, {len(invalid)} invalid (excluded), "
        f"{sum(1 for r in valid if r.get('recall_mismatch'))} transcript/stub-log mismatches. "
        f"Variants: {', '.join(variants) or '-'}. Models: {', '.join(models) or '-'}."
    )
    if invalid:
        reasons: dict[str, int] = defaultdict(int)
        for r in invalid:
            reasons[str(r.get("error") or "unknown")[:60]] += 1
        out += ["", "Invalid cells by reason:", ""]
        out += [f"- {n} x `{why}`" for why, n in sorted(reasons.items(), key=lambda kv: -kv[1])]

    out += decision_section(valid)

    # ---- per (variant, model)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in valid:
        groups[(str(r.get("variant")), str(r.get("model")))].append(r)
    body = []
    for (v, m), rs in sorted(groups.items()):
        s = summarise_group(rs)
        body.append(
            [
                v,
                m,
                str(s["n"]),
                f"{s['n_pos']}/{s['n_neg']}",
                fmt_ci(s["tp"], s["n_pos"]),
                fmt_ci(s["fp"], s["n_neg"]),
                fmt(s["bal_acc"]),
                fmt(s["mean_phrasings"], 1) + f" (n={s['n_recall']})",
                fmt(s["first_recall_rate"]),
                str(s["mismatch"]),
            ]
        )
    out += [
        "",
        "## Per variant and model",
        "",
        "TPR = recall elected on positives, FPR = recall elected on negatives, both with "
        "Wilson 95% intervals. Balanced accuracy = (TPR + (1 - FPR)) / 2. Phrasings = mean "
        "queries per recall call. First = share of positives whose first tool call was recall. "
        "Mismatch = cells where the transcript and the stub's call log disagree (should be 0).",
        "",
        table(
            [
                "variant",
                "model",
                "n",
                "pos/neg",
                "TPR",
                "FPR",
                "bal. acc",
                "phrasings",
                "first",
                "mismatch",
            ],
            body,
        ),
    ]

    # ---- per (variant, model, class)
    classes = sorted({str(r.get("class") or "") for r in valid})
    if any(classes) and len(classes) > 1:
        body = []
        for (v, m), rs in sorted(groups.items()):
            cells = [v, m]
            for c in classes:
                sub = [r for r in rs if str(r.get("class") or "") == c]
                k = sum(1 for r in sub if r.get("recall_called"))
                cells.append(f"{fmt(rate(k, len(sub)))} ({k}/{len(sub)})" if sub else "-")
            body.append(cells)
        out += [
            "",
            "## Election rate per class",
            "",
            "Share of cells in each scenario class where recall was called (positives should be "
            "high, negatives low; the class says which).",
            "",
            table(["variant", "model", *classes], body),
        ]

    # ---- per scenario across variants
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in valid:
        by_scenario[str(r.get("scenario"))].append(r)
    body = []
    n_flagged = 0
    for sid, rs in sorted(by_scenario.items()):
        k = sum(1 for r in rs if r.get("recall_called"))
        n = len(rs)
        per_variant = []
        for v in variants:
            sub = [r for r in rs if str(r.get("variant")) == v]
            if sub:
                kv = sum(1 for r in sub if r.get("recall_called"))
                per_variant.append(f"{v}={kv / len(sub):.2f}")
        flag = ""
        if n >= 2 and k in (0, n):
            flag = "no signal (all 0)" if k == 0 else "no signal (all 1)"
            n_flagged += 1
        expect = rs[0].get("expect")
        body.append(
            [
                sid,
                str(rs[0].get("class") or ""),
                "recall" if expect else "no recall",
                str(n),
                fmt_ci(k, n),
                " ".join(per_variant),
                flag,
            ]
        )
    out += [
        "",
        "## Per scenario across variants",
        "",
        f"Election rate pooled over variants, models and repeats. {n_flagged} scenario(s) show no "
        "signal: every variant elects (or never elects) them, so they cannot separate variants; "
        "consider replacing them.",
        "",
        table(["scenario", "class", "expect", "n", "election rate", "per variant", "flag"], body),
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("results", type=Path, nargs="?", default=Path("results.jsonl"))
    ap.add_argument("--report", type=Path, default=None, help="default: report.md beside results")
    args = ap.parse_args(argv)
    if not args.results.is_file():
        raise SystemExit(f"{args.results}: no such file")
    rows = load_rows(args.results)
    if not rows:
        raise SystemExit(f"{args.results}: no rows")
    report = build_report(rows, args.results)
    target = args.report or args.results.resolve().parent / "report.md"
    target.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nwrote {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
