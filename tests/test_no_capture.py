"""``MEMWARE_NO_CAPTURE`` reaches only the processes of the session that sets it.

The 2026-09-11 repro: a no-capture session that its own ``SessionEnd`` hook skipped was indexed
by the next session's ``SessionStart`` catch-up and copied by the backup mirror, and neither of
those ever saw the variable. A hook that does see it now puts the transcript on the no-capture
list, and every sync and every mirror honours the list. These tests encode that chain, the list's
own guarantees, and the mirror's handling of marker-tagged transcripts.
"""

from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path

import pytest

from memware import backup as bk
from memware.cli import main
from memware.config import memware_home
from memware.digest import project_dir_name
from memware.ingest import (
    is_no_capture,
    no_capture_file,
    no_capture_paths,
    parser_for,
    record_no_capture,
    sync_file,
)
from memware.ledger import assert_belief
from memware.store import Store
from tests.conftest import write_claude_jsonl

SECRET = "patient MRN 4417 was admitted on the ninth"
NOTED = "noted, the admission is on file"
SUBAGENT = "looked up the admission record for MRN 4417"


def _turns_from(db: str | Path, path: Path) -> int:
    with Store(db) as s:
        row = s.conn.execute("SELECT count(*) FROM turn WHERE source=?", (str(path.resolve()),))
        return int(row.fetchone()[0])


def _hook(monkeypatch, capsys, argv: list[str], payload: dict) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    assert main(argv) == 0
    return capsys.readouterr().out


@pytest.fixture()
def machine(tmp_path):
    """A transcript tree with one ordinary session, a backup destination, and the config the
    plugin's catch-up and backup read. The no-capture session's transcript is not written yet."""
    projects = tmp_path / "projects"
    (projects / "p").mkdir(parents=True)
    other = projects / "p" / "other.jsonl"
    write_claude_jsonl(
        other, "other", [("assistant", "2026-09-10T00:00:00Z", "the build caches under .build")]
    )
    dest = tmp_path / "dropbox" / "memware"
    memware_home().mkdir(parents=True)
    (memware_home() / "config.json").write_text(
        json.dumps({"backup": {"dest": str(dest), "transcript_src": str(projects)}})
    )
    return {
        "db": str(tmp_path / "m.db"),
        "projects": projects,
        "secret": projects / "p" / "secret.jsonl",
        "other": other,
        "dest": dest,
    }


@pytest.mark.parametrize(
    ("argv", "event"),
    [
        (["notice", "--from-hook"], "SessionStart"),
        (["digest", "--from-hook"], "SessionStart"),
        (["context", "--from-hook"], "UserPromptSubmit"),
    ],
)
def test_a_session_one_hook_saw_is_never_indexed_or_mirrored(
    machine, monkeypatch, capsys, argv, event
):
    """The repro, with the session force-killed: no SessionEnd ran, so a start or prompt hook
    was the only memware process that saw the variable. The next session's catch-up, a
    backfill and a backup all run without it."""
    db, secret = machine["db"], machine["secret"]
    monkeypatch.setenv("MEMWARE_NO_CAPTURE", "1")
    _hook(
        monkeypatch,
        capsys,
        ["--db", db, *argv],
        {
            "hook_event_name": event,
            "session_id": "secret",
            "transcript_path": str(secret),
            "cwd": str(machine["projects"]),
            "source": "startup",
            "prompt": "hi",
        },
    )
    write_claude_jsonl(  # the session then runs, and is killed
        secret,
        "secret",
        [("user", "2026-09-11T00:00:00Z", SECRET), ("assistant", "2026-09-11T00:00:05Z", NOTED)],
    )
    subagent = machine["projects"] / "p" / "secret" / "subagents" / "agent-a1.jsonl"
    subagent.parent.mkdir(parents=True)
    write_claude_jsonl(subagent, "secret", [("assistant", "2026-09-11T00:00:03Z", SUBAGENT)])
    monkeypatch.delenv("MEMWARE_NO_CAPTURE")
    parse = parser_for("claude-code")
    assert (len(list(parse(secret, 0))), len(list(parse(subagent, 0)))) == (2, 1)  # indexable

    assert main(["--db", db, "sync", "--harness", "claude-code"]) == 0  # SessionStart catch-up
    assert main(["--db", db, "backfill", str(machine["projects"])]) == 0
    capsys.readouterr()
    assert main(["--db", db, "backup", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)

    assert _turns_from(db, secret) == 0 and _turns_from(db, subagent) == 0
    assert _turns_from(db, machine["other"]) == 1  # the catch-up did run
    assert not (machine["dest"] / "transcripts" / "p" / "secret.jsonl").exists()
    assert not (machine["dest"] / "transcripts" / "p" / "secret").exists()
    assert (machine["dest"] / "transcripts" / "p" / "other.jsonl").exists()
    assert (out["transcripts_mirrored"], out["transcripts_skipped_no_capture"]) == (1, 2)


def test_hook_sync_under_the_variable_records_and_skips(machine, monkeypatch, capsys):
    db, secret = machine["db"], machine["secret"]
    write_claude_jsonl(secret, "secret", [("user", "2026-09-11T00:00:00Z", SECRET)])
    monkeypatch.setenv("MEMWARE_NO_CAPTURE", "1")

    out = _hook(
        monkeypatch, capsys, ["--db", db, "sync", "--from-hook"], {"transcript_path": str(secret)}
    )

    assert out == ""
    assert no_capture_paths() == {str(secret.resolve())}
    assert _turns_from(db, secret) == 0
    monkeypatch.delenv("MEMWARE_NO_CAPTURE")
    assert main(["--db", db, "sync", str(secret)]) == 0  # an explicit path is refused too
    assert _turns_from(db, secret) == 0


def test_without_the_variable_nothing_is_recorded(machine, monkeypatch, capsys):
    db, secret = machine["db"], machine["secret"]
    write_claude_jsonl(
        secret, "secret", [("user", "2026-09-11T00:00:00Z", "an ordinary prompt about the build")]
    )
    _hook(
        monkeypatch, capsys, ["--db", db, "sync", "--from-hook"], {"transcript_path": str(secret)}
    )

    assert not no_capture_file().exists()
    assert _turns_from(db, secret) == 1


def _prepare_digest(tmp_path, monkeypatch) -> dict:
    """A store holding one earlier session for the cwd's project, so the digest has a block."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    folder = tmp_path / "claude" / "projects" / project_dir_name(str(cwd))
    folder.mkdir(parents=True)
    write_claude_jsonl(
        folder / "old.jsonl",
        "old",
        [("user", "2026-09-10T10:00:00Z", "wire the retry policy into the uploader")],
    )
    with Store(tmp_path / "m.db") as s:
        sync_file(s, folder / "old.jsonl", harness="claude-code")
    return {"cwd": str(cwd), "transcript_path": str(folder / "new.jsonl"), "session_id": "new"}


def _prepare_context(tmp_path, monkeypatch) -> dict:
    with Store(tmp_path / "m.db") as s:
        assert_belief(s, "api", "listens on port", "8443")
    return {"prompt": "which port does the api listen on"}


def _prepare_notice(tmp_path, monkeypatch) -> dict:
    memware_home().mkdir(parents=True, exist_ok=True)
    (memware_home() / "config.json").write_text(
        json.dumps({"setup": {"completed_version": "0.2.5"}})
    )
    return {"source": "startup"}


@pytest.mark.parametrize(
    ("argv", "prepare"),
    [
        (["notice", "--from-hook"], _prepare_notice),
        (["digest", "--from-hook"], _prepare_digest),
        (["context", "--from-hook"], _prepare_context),
    ],
)
def test_recording_never_changes_hook_output(tmp_path, monkeypatch, capsys, argv, prepare):
    payload = prepare(tmp_path, monkeypatch)
    payload.setdefault("transcript_path", str(tmp_path / "projects" / "p" / "s.jsonl"))
    cmd = ["--db", str(tmp_path / "m.db"), *argv]

    plain = _hook(monkeypatch, capsys, cmd, payload)
    assert plain.strip() and not no_capture_file().exists()
    monkeypatch.setenv("MEMWARE_NO_CAPTURE", "1")
    listed = _hook(monkeypatch, capsys, cmd, payload)

    assert listed == plain
    assert no_capture_paths() == {str(Path(payload["transcript_path"]).resolve())}


def test_a_transcript_indexed_before_it_was_listed_is_pruned(store, tmp_path):
    p = tmp_path / "s.jsonl"
    write_claude_jsonl(
        p,
        "s",
        [("user", "2026-09-11T00:00:00Z", SECRET), ("assistant", "2026-09-11T00:00:05Z", NOTED)],
    )
    assert sync_file(store, p, harness="claude-code") == 2

    record_no_capture(p)

    assert sync_file(store, p, harness="claude-code") == 0
    assert store.stats()["turns"] == 0 and store.stats()["passages"] == 0
    assert store.conn.execute("SELECT count(*) FROM cursor").fetchone()[0] == 0


def test_record_resolves_dedupes_and_replaces_the_file_whole(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)

    assert record_no_capture(tmp_path / "link" / "s.jsonl") is True  # need not exist yet
    assert record_no_capture(real / "s.jsonl") is False
    assert record_no_capture(tmp_path / "t.jsonl") is True

    lines = no_capture_file().read_text(encoding="utf-8").splitlines()
    assert lines == [str((real / "s.jsonl").resolve()), str((tmp_path / "t.jsonl").resolve())]
    assert {p.name for p in no_capture_file().parent.iterdir()} == {
        "no-capture.txt",
        "no-capture.txt.lock",
    }


def test_a_listed_session_covers_its_subagents_and_nothing_beside_them(tmp_path):
    """Claude Code keeps a session's subagent transcripts in ``<session>/subagents/``, beside
    ``<session>.jsonl``, with the session's id on their turns."""
    p = tmp_path / "p"
    record_no_capture(p / "abc.jsonl")
    listed = no_capture_paths()

    def listed_(rel: str) -> bool:
        return is_no_capture((p / rel).resolve(), listed)

    assert listed_("abc.jsonl")
    assert listed_("abc/subagents/agent-1.jsonl")
    assert listed_("abc/subagents/workflows/wf_1/agent-2.jsonl")
    assert not listed_("abcd.jsonl")
    assert not listed_("abcd/subagents/agent-3.jsonl")
    assert not listed_("other.jsonl")
    assert not is_no_capture((p / "abc.jsonl").resolve(), set())


def test_sessions_that_record_at_once_all_land(tmp_path):
    """Eval harnesses start many sessions together. Two writers that each read the list and
    replace it would drop one of the two paths without the lock."""
    paths = [tmp_path / f"s{i}.jsonl" for i in range(160)]

    def record(chunk: list[Path]) -> None:
        for p in chunk:
            record_no_capture(p)

    threads = [threading.Thread(target=record, args=(paths[i::8],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = no_capture_file().read_text(encoding="utf-8").splitlines()
    assert sorted(lines) == sorted(str(p.resolve()) for p in paths)


def _tree(tmp_path) -> tuple[Path, Path, dict[str, Path]]:
    src, dest = tmp_path / "projects", tmp_path / "dropbox"
    (src / "p").mkdir(parents=True)
    files = {name: src / "p" / f"{name}.jsonl" for name in ("good", "secret", "eval")}
    write_claude_jsonl(
        files["good"], "g", [("user", "2026-09-11T00:00:00Z", "ordinary work on the build cache")]
    )
    write_claude_jsonl(files["secret"], "s", [("user", "2026-09-11T00:00:00Z", SECRET)])
    files["subagent"] = src / "p" / "secret" / "subagents" / "agent-a1.jsonl"
    files["subagent"].parent.mkdir(parents=True)
    write_claude_jsonl(files["subagent"], "s", [("assistant", "2026-09-11T00:00:03Z", SUBAGENT)])
    write_claude_jsonl(
        files["eval"], "e", [("user", "2026-09-11T00:00:00Z", "[memware-eval] which port")]
    )
    memware_home().mkdir(parents=True, exist_ok=True)
    (memware_home() / "ignore-markers.txt").write_text("[memware-eval]\n")
    record_no_capture(files["secret"])
    return src, dest, files


def test_mirror_leaves_out_listed_and_marked_transcripts_and_deletes_nothing(tmp_path):
    src, dest, files = _tree(tmp_path)
    earlier = dest / "transcripts" / "p" / "secret.jsonl"  # copied before the session was listed
    earlier.parent.mkdir(parents=True)
    earlier.write_text("an old copy")

    res = bk.mirror_transcripts(src, dest)

    assert res.copied == 1 and res.skipped == []
    assert sorted(res.excluded_no_capture) == sorted([files["secret"], files["subagent"]])
    assert res.excluded_marker == [files["eval"]]
    assert res.left_in_backup == [earlier]
    assert earlier.read_text() == "an old copy"  # reported, never deleted
    assert sorted(p.name for p in earlier.parent.iterdir()) == ["good.jsonl", "secret.jsonl"]


@pytest.mark.parametrize("fmt", [["--json"], []], ids=["json", "human"])
def test_backup_reports_what_it_left_out(tmp_path, capsys, fmt):
    src, dest, _ = _tree(tmp_path)
    earlier = dest / "transcripts" / "p" / "eval.jsonl"  # copied before the marker was listed
    earlier.parent.mkdir(parents=True)
    earlier.write_text("an old copy")
    db = str(tmp_path / "m.db")
    Store(db).close()
    capsys.readouterr()

    args = [
        "--db",
        db,
        "backup",
        "--dest",
        str(dest),
        "--transcripts",
        "--transcript-src",
        str(src),
    ]
    assert main([*args, *fmt]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)  # the human output is the same record on one line

    assert out["transcripts_mirrored"] == 1
    assert (out["transcripts_skipped_no_capture"], out["transcripts_skipped_marker"]) == (2, 1)
    assert out["transcripts_left_in_backup"] == [str(earlier)]
    assert f"remove it by hand: {earlier}" in cap.err
    assert earlier.read_text() == "an old copy"
