"""The garden skill's script (integrations/claude-code/skills/garden/garden.py) on a synthetic
relevance log and synthetic transcripts: it must never print a prompt or a fact, must label each
pair once across cycles, and must turn verdicts into the moves its objective and checks promise."""

import datetime as dt
import importlib.util
import json
import os
import random
import sys
import sysconfig
from pathlib import Path

import pytest

from memware.digest import belief_line
from memware.ledger import assert_belief
from memware.relevance import prompt_id, sent_prompt
from memware.store import Store
from tests.conftest import write_hook_transcript

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "integrations" / "claude-code" / "skills" / "garden" / "garden.py"
SECRET = "zebra-quartz"  # in every prompt and fact; must never reach stdout


@pytest.fixture()
def garden():
    spec = importlib.util.spec_from_file_location("garden", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def transcripts_here(monkeypatch):
    """garden.py runs `memware injections`: the one installed with this checkout, reading a
    fixture tree, never the developer's own transcripts."""
    exe = Path(sysconfig.get_path("scripts")) / ("memware.exe" if os.name == "nt" else "memware")
    if not exe.exists():
        pytest.skip("the memware console script is not installed beside this interpreter")
    monkeypatch.setenv("MEMWARE_CLI", str(exe))
    transcripts().mkdir(parents=True, exist_ok=True)
    write_config(mode="off")


def home() -> Path:
    h = Path(os.environ["MEMWARE_HOME"])
    h.mkdir(parents=True, exist_ok=True)
    return h


def transcripts() -> Path:
    return home().parent / "transcripts"


def write_config(threshold=0.3, pool=20, k=None, mode="filter"):
    cfg = {"relevance": {"mode": mode, "threshold": threshold, "pool": pool, "timeout_s": 2.0}}
    if k is not None:
        cfg["inject"] = {"k": k}
    cfg["backup"] = {"transcript_src": str(transcripts())}
    (home() / "config.json").write_text(json.dumps(cfg))


def write_log(n_prompts=300, pool=20, k=6, threshold=0.3, mode="filter", seed=1, errors=0):
    """Each prompt: a pool of candidates with scores; memware's own picks are ranks < 4; the
    filter chose p >= threshold, best first, at most k. Returns every row, for truth functions."""
    rng = random.Random(seed)
    now = dt.datetime.now(dt.UTC)
    lines, usage = [], []
    for i in range(n_prompts):
        ts = (now - dt.timedelta(minutes=n_prompts - i)).isoformat()
        pid = f"{rng.getrandbits(64):016x}"
        failed = i < errors
        ps = [None if failed else round(rng.random(), 3) for _ in range(pool)]
        chosen = set()
        if not failed:
            above = sorted((j for j in range(pool) if ps[j] >= threshold), key=lambda j: -ps[j])
            chosen = set(above[:k])
        for rank in range(pool):
            lines.append(
                {
                    "ts": ts,
                    "harness": "claude-code",
                    "mode": mode,
                    "model": "jev-1.13.0",
                    "pair_id": f"{pid}:{rank + 100 * (i % 7)}",
                    "prompt_id": pid,
                    "belief_id": rank + 100 * (i % 7),
                    "rank": rank,
                    "today": rank < 4,
                    "p": ps[rank],
                    "threshold": threshold,
                    "chosen": None if failed else rank in chosen,
                    "error": "timeout" if failed else None,
                    "ms": 400,
                    "session": "s",
                    "prompt": f"prompt {i} {SECRET}",
                    "fact": f"fact {rank} {SECRET}",
                }
            )
        if not failed:
            usage.append(
                {
                    "ts": ts,
                    "process": "memware-relevance",
                    "harness": "claude-code",
                    "model": "jev-1.13.0",
                    "input_tokens": 3000,
                    "cost_usd": 0.0001,
                    "latency_ms": 400,
                }
            )
    h = home()
    (h / "relevance-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    (h / "relevance-usage.jsonl").write_text("".join(json.dumps(u) + "\n" for u in usage))
    write_config(threshold=threshold, pool=pool, mode=mode)
    return {r["pair_id"]: r for r in lines}


def by_score(rows):
    """Truth: a fact is relevant when the filter scored it at least 0.6."""
    return lambda pair_id: rows[pair_id]["p"] >= 0.6


def label(work: Path, truth, passes=("A", "B"), flip=frozenset()):
    """Stand in for the labeling agents; pass B disagrees on `flip`."""
    for b in sorted(work.glob("batch*.jsonl")):
        ids = [
            f["pair_id"] for line in b.read_text().splitlines() for f in json.loads(line)["facts"]
        ]
        for p in passes:
            rows = [
                {
                    "pair_id": i,
                    "relevant": truth(i) != (p == "B" and i in flip),
                    "confidence": "high",
                    "reason": "x",
                }
                for i in ids
            ]
            (work / "labels" / f"{b.stem}-{p}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows)
            )


def run(garden, capsys, *argv):
    assert garden.main(list(argv)) == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    return json.loads(out)


def cycle(garden, capsys, truth, record=False):
    work = home() / "labels" / "work-1"
    run(garden, capsys, "sample", "--work", str(work), "--max-pairs", "100000")
    label(work, truth)
    args = ["score", "--work", str(work)] + (["--record"] if record else [])
    return run(garden, capsys, *args)


def settings(r):
    return {p["set"]: (p["from"], p["to"]) for p in r["proposals"] if "set" in p}


def test_health_reports_rates_without_text(garden, capsys):
    write_log(n_prompts=120, errors=3)
    h = run(garden, capsys, "health", "--days", "14")
    assert h["prompts"] == 120 and h["answered"] == 117
    assert h["fell_back"] == {"timeout": 3}
    assert h["calls"] == 117 and h["latency_ms_p50"] == 400
    assert h["pairs"]["kept"] + h["pairs"]["dropped"] == 117 * 4
    assert h["settings"]["k"] == 6


def test_a_full_cycle_moves_halfway_to_the_joint_optimum(garden, capsys):
    rows = write_log()
    work = home() / "labels" / "work-1"
    s = run(garden, capsys, "sample", "--work", str(work), "--days", "14", "--max-pairs", "100000")
    if sys.platform != "win32":  # POSIX mode bits; Windows keeps the user profile's ACL
        assert oct(work.stat().st_mode)[-3:] == "700"
        assert all(oct(Path(b["file"]).stat().st_mode)[-3:] == "600" for b in s["batches"])
    # Labelers never see scores or groups.
    batch = Path(s["batches"][0]["file"]).read_text()
    assert '"p"' not in batch and '"group"' not in batch

    ids = [
        f["pair_id"]
        for b in s["batches"]
        for line in Path(b["file"]).read_text().splitlines()
        for f in json.loads(line)["facts"]
    ]
    flip = set(ids[:10])
    truth = by_score(rows)
    label(work, truth, flip=flip)
    d = run(garden, capsys, "disputes", "--work", str(work))
    assert d["disputed_pairs"] == 10
    label_c = [
        {"pair_id": i, "relevant": truth(i), "confidence": "low", "reason": "x"} for i in flip
    ]
    (work / "labels" / "batchdisputes-C.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in label_c)
    )

    r = run(garden, capsys, "score", "--work", str(work), "--days", "14", "--record")
    assert r["new_verdicts"] == len(ids)
    rp = r["replay"]
    assert rp["complete_prompts"] == 300 and rp["split"] == "by prompt hash"
    # Truth is p >= 0.6, so the optimum raises the threshold to the grid's top and lets more in.
    assert (rp["optimum"]["threshold"], rp["optimum"]["k"], rp["optimum"]["noise"]) == (0.6, 10, 0)
    assert rp["optimum"]["relevant"] >= rp["current"]["relevant"]
    # Damped: the smallest move that improves on the objective and holds on the held-out prompts.
    # Between 0.30 and 0.45 nothing changes here (the top six all clear 0.45), so it goes to 0.5.
    assert settings(r) == {"relevance.threshold": (0.3, 0.5)}
    assert r["replay"]["move"]["noise"] < r["replay"]["current"]["noise"]
    history = garden._read_jsonl(home() / "labels" / "garden" / "history.jsonl")
    assert len(history) == 1 and SECRET not in json.dumps(history)
    assert SECRET not in (home() / "labels" / "garden" / "verdicts.jsonl").read_text()

    # Every prompt is complete now, so the next cycle has nothing to label.
    again = run(garden, capsys, "sample", "--work", str(home() / "labels" / "work-2"))
    assert again["pairs"] == 0

    run(garden, capsys, "clean", "--work", str(work))
    assert not work.exists()


def test_cycles_converge_on_the_optimum_and_only_then_look_past_the_pool(garden, capsys):
    """Adopting each move and reporting again walks to the joint optimum, several settings at a
    time, and a wider pool is proposed only once nothing else moves."""
    rows = write_log()
    cycle(garden, capsys, by_score(rows))
    path = []
    for _ in range(8):
        r = run(garden, capsys, "report")
        path.append(settings(r))
        mv = r["replay"].get("move")
        if not mv:
            break
        write_config(threshold=mv["threshold"], k=mv["k"], pool=mv["pool"])
    assert "already the best" in r["replay"]["verdict"]
    cur = r["replay"]["current"]
    assert (cur["threshold"], cur["k"], cur["pool"], cur["noise"]) == (0.6, 10, 20, 0)
    assert any(len(step) > 1 for step in path)  # at least one cycle moved two settings together
    assert path[-1] == {"relevance.pool": (20, 30)}
    assert all("relevance.pool" not in step for step in path[:-1])


def test_a_move_the_held_out_prompts_contradict_is_not_made(garden, capsys):
    """On the held-out third the filter's scores run backwards (0.3 to 0.6 is relevant, above it
    noise), so the optimum found on the rest must not be adopted."""
    rows = write_log()

    def truth(pair_id):
        r = rows[pair_id]
        if int(r["prompt_id"][:8], 16) % 3 == 0:
            return 0.3 <= r["p"] < 0.6
        return r["p"] >= 0.6

    r = cycle(garden, capsys, truth)
    assert "nothing moves" in r["replay"]["verdict"]
    assert settings(r) == {}  # and no wider pool either: nothing has settled


def test_too_few_labels_move_nothing(garden, capsys):
    rows = write_log(n_prompts=30)
    r = cycle(garden, capsys, by_score(rows))
    assert "not enough complete prompts" in r["replay"]["verdict"]
    assert settings(r) == {}


def test_timeouts_propose_a_longer_timeout(garden, capsys):
    write_log(n_prompts=200, errors=10)
    r = run(garden, capsys, "report", "--days", "14")
    assert settings(r)["relevance.timeout_s"] == (2.0, 2.5)


def test_score_refuses_a_label_file_that_misses_pairs(garden, capsys):
    rows = write_log(n_prompts=10)
    work = home() / "labels" / "work-1"
    run(garden, capsys, "sample", "--work", str(work))
    label(work, by_score(rows))
    f = next((work / "labels").glob("*-A.jsonl"))
    f.write_text("".join(f.read_text().splitlines(keepends=True)[1:]))
    assert garden.main(["score", "--work", str(work)]) == 1
    assert "label_file_problems" in capsys.readouterr().out
    assert not (home() / "labels" / "garden" / "verdicts.jsonl").exists()


def test_clean_removes_only_a_directory_sample_made(garden, capsys, tmp_path):
    other = tmp_path / "keep-me"
    other.mkdir()
    (other / "x").write_text("x")
    run(garden, capsys, "clean", "--work", str(other))
    assert (other / "x").exists()


def test_lines_whose_text_was_trimmed_are_replayed_but_not_sampled(garden, capsys):
    rows = write_log(n_prompts=20)
    log = home() / "relevance-log.jsonl"
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    trimmed = {r["prompt_id"] for r in lines[:100]}  # the first five prompts
    for r in lines[:100]:
        r["prompt"] = r["fact"] = None
    log.write_text("".join(json.dumps(r) + "\n" for r in lines))
    work = home() / "labels" / "work-1"
    run(garden, capsys, "sample", "--work", str(work), "--max-pairs", "100000")
    key = garden._read_jsonl(work / "key.jsonl")
    assert key and not {k["pair_id"].split(":")[0] for k in key} & trimmed
    assert rows  # the replay still reads every line's scores
    assert len(garden.prompts(garden.log_rows(None))) == 20


# --- the transcripts: relevance.mode off ---------------------------------------------------


def write_injections(n_prompts=240, k=6, per_rank=10):
    """A ledger of ``per_rank`` beliefs for each rank, and one session in which the hook injected
    ``k`` of them into each prompt, rank r drawn from rank r's beliefs. Returns pair_id -> rank."""
    s = Store(home() / "memware.db")
    beliefs = [
        [
            assert_belief(
                s, f"thing{r}x{j}", "is", f"value {SECRET}", valid_from="2026-09-01T00:00:00Z"
            ).belief_id
            for j in range(per_rank)
        ]
        for r in range(k)
    ]
    lines = {}
    for r in range(k):
        for b in beliefs[r]:
            row = s.conn.execute("SELECT * FROM belief WHERE id=?", (b,)).fetchone()
            lines[b] = belief_line(row["subject"], row["relation"], row["value"], row["valid_from"])
    s.close()
    now = dt.datetime.now(dt.UTC)
    prompts, rank_of = [], {}
    for i in range(n_prompts):
        ts = (now - dt.timedelta(minutes=n_prompts - i)).isoformat().replace("+00:00", "Z")
        text = f"prompt {i} {SECRET}"
        injected = [beliefs[r][(i + r) % per_rank] for r in range(k)]
        prompts.append((ts, text, [lines[b] for b in injected]))
        pid = prompt_id(sent_prompt(text))
        rank_of |= {f"{pid}:{b}": r for r, b in enumerate(injected)}
    write_hook_transcript(transcripts() / "proj" / "s1.jsonl", "s1", prompts)
    return rank_of


def test_off_mode_health_counts_the_transcripts(garden, capsys):
    write_injections(n_prompts=20)
    h = run(garden, capsys, "health")
    t = h["transcripts"]
    assert t["read"] is True
    assert (t["prompts"], t["facts"], t["per_prompt"], t["full_blocks"]) == (20, 120, 6.0, 20)
    assert t["lines_unmatched"] == 0
    assert h["prompts"] == 0  # no relevance log in off mode
    assert h["prompts_with_unlabeled_pairs"] == 20


def test_off_mode_cycle_lowers_k_and_never_proposes_a_threshold(garden, capsys):
    """Ranks 4 and 5 are always noise. The whole cycle runs from the transcripts: sample, label,
    score. The k replay may only move down, damped, and names what it cannot tune."""
    rank_of = write_injections()
    work = home() / "labels" / "work-1"
    s = run(garden, capsys, "sample", "--work", str(work), "--max-pairs", "100000")
    assert s["prompts_from_transcripts"] == 240 and s["prompts_from_log"] == 0
    assert s["groups"] == {"injected": 240 * 6}
    label(work, lambda pair_id: rank_of[pair_id] < 4)
    r = run(garden, capsys, "score", "--work", str(work), "--record")
    assert r["sources"] == {"log": 0, "transcripts": 240 * 6}
    assert r["memware_alone"]["precision"] == round(4 / 6, 3)
    rp = r["replay"]
    assert rp["axis"] == "inject.k, downward only"
    assert rp["optimum"]["k"] == 4 and rp["optimum"]["noise"] == 0
    assert settings(r) == {"inject.k": (6, 5)}  # halfway, held on the held-out third
    assert not any("relevance." in p.get("set", "") for p in r["proposals"])
    assert any("threshold and pool are not tuned" in p.get("note", "") for p in r["proposals"])
    # The beliefs at ranks 4 and 5 keep arriving as noise.
    assert len(r["offenders_injected"]) == 20
    history = garden._read_jsonl(home() / "labels" / "garden" / "history.jsonl")
    assert history[-1]["sources"]["transcripts"] == 240 * 6
    assert SECRET not in json.dumps(history)

    write_config(mode="off", k=5)
    again = run(garden, capsys, "report")
    assert settings(again) == {"inject.k": (5, 4)}


def test_a_prompt_the_filter_scored_is_read_from_the_log_not_the_transcripts(garden, capsys):
    write_injections(n_prompts=5)
    scored = {next(iter(garden.injected_prompts(None)))}
    assert len(garden.unfiltered(None, scored)) == 4


def test_missing_memware_cli_is_reported_not_fatal(garden, capsys, monkeypatch):
    monkeypatch.setenv("MEMWARE_CLI", str(home() / "no-such-memware"))
    write_injections(n_prompts=3)
    h = run(garden, capsys, "health")
    assert h["transcripts"]["read"] is False and h["transcripts"]["prompts"] == 0
