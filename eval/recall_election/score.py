"""Score recall-election results.

Reads the JSONL run.py writes and reports, per (variant, model): election rate on positives
(TPR), false-election rate on negatives (FPR), balanced accuracy, Wilson 95% intervals, mean
phrasings of the first recall call on positives, and how often recall was the first tool on
positives. Then, per scenario across variants, the election rate, flagging scenarios with no
signal (0 or 1 everywhere); per model, a paired sign test of each variant against control; and
the decision for the candidate. Invalid cells (a tool input outside the cell's copy, timeouts,
non-zero exit, is_error) are excluded and counted by reason.

    uv run python eval/recall_election/score.py results.jsonl   # writes report.md beside it

The last stdout line is the verdict: ``VERDICT: SHIP synthesized`` or ``VERDICT: KEEP control``.
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
CONTROL = "control"
CANDIDATE = "synthesized"
ALPHA = 0.05
MAX_CELLS_LISTED = 6


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


def invalid_reason(row: dict[str, Any]) -> str:
    """The reason category run.py records; derived for rows written before it did."""
    if row.get("invalid_reason"):
        return str(row["invalid_reason"])
    error = str(row.get("error") or "")
    subtype = row.get("result_subtype")
    if error.startswith("timeout"):
        return "timeout"
    if subtype and subtype != "success":
        return str(subtype)
    if row.get("rc") not in (None, 0):
        return f"rc {row['rc']}"
    return error.split(":", 1)[0].strip() or "unknown"


def cell_id(row: dict[str, Any]) -> str:
    return "/".join(str(row.get(k)) for k in ("variant", "scenario", "model", "repeat"))


def summarise_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pos = [r for r in rows if r.get("expect")]
    neg = [r for r in rows if not r.get("expect")]
    tp = sum(1 for r in pos if r.get("recall_called"))
    fp = sum(1 for r in neg if r.get("recall_called"))
    tpr, fpr = rate(tp, len(pos)), rate(fp, len(neg))
    bal = math.nan if math.isnan(tpr) or math.isnan(fpr) else (tpr + (1 - fpr)) / 2
    # Over positives only: a negative that elects recall is already an error, and its
    # phrasings say nothing about how well the description teaches the query shape.
    phr = [int(r.get("n_phrasings") or 0) for r in pos if r.get("recall_called")]
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
        "count_mismatch": sum(1 for r in rows if r.get("count_mismatch")),
    }


# ------------------------------------------------------------------------ paired sign test


def is_correct(row: dict[str, Any]) -> bool:
    """Recall elected exactly when the scenario expects it."""
    return bool(row.get("recall_called")) == bool(row.get("expect"))


def sign_test_p(b: int, c: int) -> float:
    """Exact two-sided binomial p (q = 0.5) for b discordant pairs one way and c the other."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / float(2**n)
    return min(1.0, 2 * tail)


def paired(valid: list[dict[str, Any]], variant: str, control: str, model: str) -> dict[str, Any]:
    """Sign test of ``variant`` against ``control`` on ``model``'s matched (scenario, repeat)."""

    def index(v: str) -> dict[tuple[str, int], dict[str, Any]]:
        return {
            (str(r.get("scenario")), int(r.get("repeat") or 0)): r
            for r in valid
            if str(r.get("variant")) == v and str(r.get("model")) == model
        }

    var, ctl = index(variant), index(control)
    keys = sorted(var.keys() & ctl.keys())
    var_better = sum(1 for k in keys if is_correct(var[k]) and not is_correct(ctl[k]))
    ctl_better = sum(1 for k in keys if is_correct(ctl[k]) and not is_correct(var[k]))
    pos = [k for k in keys if ctl[k].get("expect")]
    return {
        "pairs": len(keys),
        "variant_better": var_better,
        "control_better": ctl_better,
        "p": sign_test_p(var_better, ctl_better),
        "n_pos": len(pos),
        "pos_elected_variant": sum(1 for k in pos if var[k].get("recall_called")),
        "pos_elected_control": sum(1 for k in pos if ctl[k].get("recall_called")),
    }


def decide(
    valid: list[dict[str, Any]],
    candidate: str = CANDIDATE,
    control: str = CONTROL,
    alpha: float = ALPHA,
) -> tuple[str, list[str]]:
    """(verdict line, one explanation line per model). SHIP only if every model passes."""
    models = sorted({str(r.get("model")) for r in valid})
    if not models:
        return f"VERDICT: KEEP {control}", ["no valid cells"]
    ship = True
    lines = []
    for m in models:
        t = paired(valid, candidate, control, m)
        better = t["variant_better"] > t["control_better"] and t["p"] < alpha
        no_drop = t["n_pos"] > 0 and t["pos_elected_variant"] >= t["pos_elected_control"]
        ship = ship and better and no_drop
        lines.append(
            f"{m}: {t['pairs']} matched pairs; discordant {t['variant_better']} for {candidate}, "
            f"{t['control_better']} for {control}, p = {t['p']:.4f} "
            f"({'better' if better else 'not better'} at p < {alpha:g}); positives elected "
            f"{t['pos_elected_variant']}/{t['n_pos']} vs {t['pos_elected_control']}/{t['n_pos']} "
            f"({'no drop' if no_drop else 'drop or no matched positives'}) -> "
            f"{'pass' if better and no_drop else 'fail'}"
        )
    return (f"VERDICT: SHIP {candidate}" if ship else f"VERDICT: KEEP {control}"), lines


# ---------------------------------------------------------------------------------- report


def table(header: list[str], body: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(cells) + " |" for cells in body]
    return "\n".join(lines)


def build_report(
    rows: list[dict[str, Any]],
    source: Path,
    candidate: str = CANDIDATE,
    control: str = CONTROL,
) -> str:
    valid = [r for r in rows if is_valid(r)]
    invalid = [r for r in rows if not is_valid(r)]
    variants = sorted({str(r.get("variant")) for r in valid})
    models = sorted({str(r.get("model")) for r in valid})

    out: list[str] = ["# Recall election report", "", f"Source: `{source}`", ""]
    out.append(
        f"{len(rows)} cells, {len(valid)} valid, {len(invalid)} invalid (excluded), "
        f"{sum(1 for r in valid if r.get('recall_mismatch'))} transcript/stub-log mismatches, "
        f"{sum(1 for r in valid if r.get('count_mismatch'))} recall count mismatches, "
        f"{sum(1 for r in rows if r.get('memory_paths'))} cells with memory paths in init. "
        f"Variants: {', '.join(variants) or '-'}. Models: {', '.join(models) or '-'}."
    )
    if invalid:
        by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in invalid:
            by_reason[invalid_reason(r)].append(r)
        out += ["", "Invalid cells by reason:", ""]
        for why, rs in sorted(by_reason.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            ids = ", ".join(cell_id(r) for r in rs[:MAX_CELLS_LISTED])
            more = f" and {len(rs) - MAX_CELLS_LISTED} more" if len(rs) > MAX_CELLS_LISTED else ""
            out.append(f"- {len(rs)} x `{why}`: {ids}{more}")

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
                str(s["count_mismatch"]),
            ]
        )
    out += [
        "",
        "## Per variant and model",
        "",
        "TPR = recall elected on positives, FPR = recall elected on negatives, both with "
        "Wilson 95% intervals. Balanced accuracy = (TPR + (1 - FPR)) / 2. Phrasings = mean "
        "queries in the first recall call, over positives that elected recall. First = share of "
        "positives whose first tool call was recall. Mismatch = cells where the transcript and "
        "the stub's call log disagree on whether recall ran; count = on how many times (both "
        "should be 0).",
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
                "phrasings (pos)",
                "first",
                "mismatch",
                "count",
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
    ]

    # ---- paired sign test against control
    out += [
        "",
        f"## Paired sign test against {control}",
        "",
        f"Per model, each variant against `{control}` on matched (scenario, repeat) cells where "
        "both are valid. A cell is correct when recall was elected exactly when the scenario "
        "expects it. Only discordant pairs count; p is the exact two-sided binomial p. Positives "
        "elected are over the same matched positive pairs.",
        "",
    ]
    others = [v for v in variants if v != control]
    if control not in variants or not others:
        out.append(f"Skipped: needs valid `{control}` cells and at least one other variant.")
    else:
        body = []
        for m in models:
            for v in others:
                t = paired(valid, v, control, m)
                body.append(
                    [
                        m,
                        v,
                        str(t["pairs"]),
                        str(t["variant_better"]),
                        str(t["control_better"]),
                        f"{t['p']:.4f}",
                        f"{t['pos_elected_variant']}/{t['n_pos']} vs "
                        f"{t['pos_elected_control']}/{t['n_pos']}",
                    ]
                )
        out.append(
            table(
                [
                    "model",
                    "variant",
                    "pairs",
                    "variant right, control wrong",
                    "control right, variant wrong",
                    "p",
                    "positives elected (variant vs control)",
                ],
                body,
            )
        )

    # ---- decision
    verdict, lines = decide(valid, candidate, control)
    out += [
        "",
        "## Decision",
        "",
        f"SHIP `{candidate}` only if, on each model, it has more correct discordant pairs than "
        f"`{control}` with sign-test p < {ALPHA:g} and its positive election rate over the "
        f"matched positives is not lower than `{control}`'s. Otherwise KEEP `{control}`.",
        "",
        *[f"- {line}" for line in lines],
        "",
        verdict,
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("results", type=Path, nargs="?", default=Path("results.jsonl"))
    ap.add_argument("--report", type=Path, default=None, help="default: report.md beside results")
    ap.add_argument("--candidate", default=CANDIDATE, help="variant the decision may ship")
    ap.add_argument("--control", default=CONTROL, help="variant the sign test compares against")
    args = ap.parse_args(argv)
    if not args.results.is_file():
        raise SystemExit(f"{args.results}: no such file")
    rows = load_rows(args.results)
    if not rows:
        raise SystemExit(f"{args.results}: no rows")
    report = build_report(rows, args.results, args.candidate, args.control)
    target = args.report or args.results.resolve().parent / "report.md"
    target.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nwrote {target}", file=sys.stderr)
    verdict, _ = decide([r for r in rows if is_valid(r)], args.candidate, args.control)
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
