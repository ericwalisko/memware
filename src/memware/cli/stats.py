"""`memware stats`: counts, derive state, capture and injection status."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from memware.cli._common import (
    AddCommand,
    _count,
    _hiding_verdict,
    _of,
    _out,
    _stale,
    _transcripts_on_disk,
)
from memware.cli.notice import _maybe_setup_hint
from memware.derive import status as derive_status
from memware.digest import injection_gate, resolve_project
from memware.ledger import orphaned_count, stale_turn_count
from memware.store import Store
from memware.volatile import REASONS, Gate, label

STALE_DERIVE_DAYS = 30


def _stats_verdicts(r: dict[str, Any]) -> list[str]:
    """One line per degenerate state a person should act on, from a ``cmd_stats`` report. Human
    output only: under --json the fields carry the same facts. The case this exists for is a
    store with thousands of turns and no beliefs, where "derive never ran" and "derive found
    nothing" otherwise print the same zero."""
    d, u = r["derive"], r["utilization"]
    out: list[str] = []
    if r["turns"] and not r["beliefs_current"] and not d["runs"]:
        out.append(
            "ledger empty: derive has never run. `memware derive --plan` previews with no "
            "network call; `memware config derive.auto true` enables it."
        )
    age = d["last_run_age_hours"]
    pending = d["turns_pending"]
    if not d["auto"] and age is not None and age > STALE_DERIVE_DAYS * 24 and pending:
        out.append(
            f"derive last ran {int(age // 24)} days ago and derive.auto is off "
            f"({pending:,} turn{'' if pending == 1 else 's'} not yet derived). "
            "`memware derive --apply` catches up; `memware config derive.auto true` keeps it "
            "current."
        )
    if r["turns"] and not u["beliefs_recalled_30d"] and not u["turns_recalled_30d"]:
        out.append("nothing has been recalled in 30 days")
    orphaned = u["beliefs_orphaned"]
    if orphaned:
        out.append(
            f"{orphaned:,} {'belief cites' if orphaned == 1 else 'beliefs cite'} a session that is "
            "no longer indexed. `memware beliefs retract --orphaned` lists them; add --apply to "
            "retract."
        )
    c = r["capture"]
    hiding = c["exclude"] and _hiding_verdict(c["transcripts_excluded"], c["transcripts"])
    if hiding:
        out.append(hiding)
    return out


def _when(ts: str | None, age: float | None, never: str) -> str:
    if not ts:
        return never
    if age is None:
        return ts
    ago = f"{age:.1f} hours" if age < 48 else f"{int(age // 24)} days"
    return f"{ts} ({ago} ago)"


def _provenance_lines(p: dict[str, Any]) -> list[tuple[str, str]]:
    """Sessions, turns and beliefs by entrypoint, then the project directories with the most
    sessions: a generator that wrote much of the store is visible without an audit."""
    home = str(Path.home())
    lines: list[tuple[str, str]] = []
    for g in p["by_entrypoint"]:
        label = f"entrypoint {g['entrypoint']}" if g["entrypoint"] else "no entrypoint"
        value = ", ".join(_count(g[k], k[:-1]) for k in ("sessions", "turns", "beliefs")) + (
            "" if g["entrypoint"] else " (derive reads these as interactive)"
        )
        lines.append((label, value))
    for d in p["top_projects"]:
        where = (
            "~" + d["directory"][len(home) :]
            if d["directory"].startswith((home + "/", home + os.sep))
            else d["directory"]
        )
        lines.append(
            ("project directory", f"{_count(d['sessions'], 'session')} ({d['share']:.1%}) {where}")
        )
    return lines


def _print_stats(r: dict[str, Any]) -> None:
    """Labeled ``field : value`` lines, a blank line between sections, verdicts last."""
    d, u = r["derive"], r["utilization"]
    share = u["turns_ever_recalled_share"]
    sections: list[list[tuple[str, str]]] = [
        [
            ("db", r["db"]),
            ("turns", f"{r['turns']:,}"),
            ("passages", f"{r['passages']:,}"),
            ("sessions", f"{r['sessions']:,}"),
            ("beliefs current", f"{r['beliefs_current']:,}"),
            ("beliefs total", f"{r['beliefs_total']:,}"),
            ("reviews open", f"{r['reviews_open']:,}"),
        ],
        _provenance_lines(r["provenance"]),
        [
            ("derive auto", "on" if d["auto"] else "off"),
            ("derive sources", d["sources"]),
            ("derive state file", d["state_file"]),
            ("derive runs", f"{d['runs']:,}"),
            ("derive last run", _when(d["last_run"], d["last_run_age_hours"], "never run")),
            ("derive watermark", f"{d['watermark']:,}"),
            ("latest turn id", f"{d['max_turn_id']:,}"),
            ("turns not yet derived", f"{d['turns_pending']:,}"),
        ],
        [
            ("beliefs recalled in 7 days", f"{u['beliefs_recalled_7d']:,}"),
            ("beliefs recalled in 30 days", f"{u['beliefs_recalled_30d']:,}"),
            ("turns recalled in 7 days", f"{u['turns_recalled_7d']:,}"),
            ("turns recalled in 30 days", f"{u['turns_recalled_30d']:,}"),
            (
                "turns ever recalled",
                f"{u['turns_ever_recalled']:,} of {r['turns']:,}"
                + ("" if share is None else f" ({share:.1%})"),
            ),
            ("last recalled", _when(u["last_recalled"], u["last_recalled_age_hours"], "never")),
            ("beliefs citing an unindexed session", f"{u['beliefs_orphaned']:,}"),
            ("beliefs with a stale turn citation", f"{u['beliefs_stale_turn']:,}"),
        ],
        _injection_lines(r["injection"]),
        _capture_lines(r["capture"]),
        [("verdict", v) for v in _stats_verdicts(r)],
    ]
    blocks = [section for section in sections if section]
    width = max(len(label) for block in blocks for label, _ in block)
    print(
        "\n\n".join(
            "\n".join(f"{label.rjust(width)} : {value}" for label, value in block)
            for block in blocks
        )
    )


def _injection_status(s: Store, gate: Gate) -> dict[str, Any]:
    """Current beliefs the injection gate leaves out, by reason, and what decided it: the window
    and, for the project in the current directory, the versions its manifests declare."""
    left_out = dict.fromkeys(REASONS, 0)
    for row in _stale(s, gate):
        left_out[row["reason"]] += 1
    return {
        "volatile_days": gate.volatile_days,
        "manifests": [d._asdict() for d in gate.declared],
        "left_out": left_out,
    }


def _injection_lines(i: dict[str, Any]) -> list[tuple[str, str]]:
    total = sum(i["left_out"].values())
    parts = ", ".join(f"{n:,} {label(k)}" for k, n in i["left_out"].items() if n)
    days = i["volatile_days"]
    lines = [
        (
            "beliefs left out of injection",
            f"{total:,}" + (f" ({parts}; `memware beliefs --stale` lists them)" if total else ""),
        ),
        (
            "inject.volatile_days",
            f"{days:g}: a volatile derived belief is injected while younger than that"
            if days
            else "0: a derived measurement, moving version or status is never injected",
        ),
    ]
    if i["manifests"]:
        declared = "; ".join(
            f"{d['name'] or '(no name)'} {d['version']} ({d['path']})" for d in i["manifests"]
        )
        lines.append(("manifest versions here", declared))
    return lines


def _capture_status() -> dict[str, Any]:
    """``capture.exclude`` and how many transcripts on disk it hides. The transcript tree is walked
    only when a pattern is set, so a store with no exclusions pays nothing."""
    from memware.config import get_dotted, load_config
    from memware.ingest import capture_exclude_patterns, is_excluded

    patterns = capture_exclude_patterns()
    out: dict[str, Any] = {"exclude": patterns, "transcripts": None, "transcripts_excluded": None}
    if patterns:
        src = str(get_dotted(load_config(), "backup.transcript_src") or "~/.claude/projects")
        disk = _transcripts_on_disk(src)
        out["transcripts"] = len(disk)
        out["transcripts_excluded"] = sum(is_excluded(p, patterns) for p in disk)
    return out


def _capture_lines(c: dict[str, Any]) -> list[tuple[str, str]]:
    if not c["exclude"]:
        return []
    n = len(c["exclude"])
    return [
        ("capture.exclude", f"{n} pattern{'' if n == 1 else 's'} (`memware exclude` lists them)"),
        ("transcripts excluded", _of(c["transcripts_excluded"], c["transcripts"])),
    ]


def cmd_stats(a: argparse.Namespace) -> int:
    _maybe_setup_hint(a)
    with Store(a.db) as s:
        report: dict[str, Any] = {"db": str(s.path), **s.stats()}
        report["provenance"] = s.provenance()
        report["derive"] = derive_status(s.conn, s.path)
        report["utilization"] = {
            **s.utilization(),
            "beliefs_orphaned": orphaned_count(s),
            "beliefs_stale_turn": stale_turn_count(s),
        }
        report["injection"] = _injection_status(s, injection_gate(resolve_project(Path.cwd())))
    report["capture"] = _capture_status()
    if a.json:
        _out(report, True)
    else:
        _print_stats(report)
    return 0


def register(add: AddCommand) -> None:
    s = add(
        "stats",
        "counts, derive state, and recall utilization; says plainly when memory is inert",
    )
    s.set_defaults(fn=cmd_stats)
