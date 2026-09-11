import io
import json
import sys

import pytest

from memware.cli import main
from tests.conftest import write_claude_jsonl


def test_cli_end_to_end(tmp_path, capsys):
    db = str(tmp_path / "c.db")
    p = tmp_path / "s.jsonl"
    write_claude_jsonl(
        p, "s", [("assistant", "2026-08-20T00:00:00Z", "the ingest job runs nightly at three")]
    )
    assert main(["--db", db, "sync", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 1
    main(["--db", db, "assert", "ingest job", "runs at", "03:00", "--json"])
    capsys.readouterr()
    assert main(["--db", db, "recall", "ingest nightly", "--json"]) == 0
    assert {h["kind"] for h in json.loads(capsys.readouterr().out)} == {"turn", "belief"}
    assert main(["--db", db, "context", "when does the ingest job run"]) == 0
    assert "03:00" in capsys.readouterr().out
    assert main(["--db", db, "stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["beliefs_current"] == 1


def test_context_from_hook_emits_hook_json(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "h.db")
    main(["--db", db, "assert", "api", "port", "8443"])
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "which api port"})))
    main(["--db", db, "context", "--from-hook"])
    out = json.loads(capsys.readouterr().out)
    assert "8443" in out["hookSpecificOutput"]["additionalContext"]


def test_backfill_indexes_existing_transcripts(tmp_path, capsys):
    import json

    from tests.conftest import write_claude_jsonl

    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    write_claude_jsonl(
        root / "s.jsonl",
        "s",
        [
            (
                "assistant",
                "2026-08-20T00:00:00Z",
                "the deploy script runs blue-green rollouts nightly",
            )
        ],
    )
    db = str(tmp_path / "b.db")
    assert main(["--db", db, "backfill", str(tmp_path / "projects"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["turns_added"] == 1 and out["sessions"] == 1
    # idempotent
    assert main(["--db", db, "backfill", str(tmp_path / "projects"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["turns_added"] == 0


def test_setup_yes_backfills_and_makes_first_backup(tmp_path, capsys, monkeypatch):
    """A fresh-install walkthrough end to end: --yes indexes the sessions already on disk and
    takes a first backup, and it marks setup done so the discovery hint stops."""
    from memware import __version__
    from memware import backup as bk
    from memware.config import get_dotted, load_config

    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    projects = tmp_path / "projects"
    (projects / "p").mkdir(parents=True)
    write_claude_jsonl(
        projects / "p" / "s.jsonl",
        "s",
        [("assistant", "2026-08-20T00:00:00Z", "the nightly job compacts the write-ahead log")],
    )
    dest = tmp_path / "dropbox" / "memware"
    db = str(tmp_path / "m.db")
    for k, v in (("backup.transcript_src", str(projects)), ("backup.dest", str(dest))):
        main(["--db", db, "config", k, str(v)])
    capsys.readouterr()

    assert main(["--db", db, "setup", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "indexed 1 turns" in out

    assert main(["--db", db, "stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["turns"] == 1  # backfill happened
    assert get_dotted(load_config(), "setup.completed_version") == __version__
    assert bk.list_snapshots(dest)  # a first snapshot was taken
    assert (dest / "transcripts" / "p" / "s.jsonl").exists()  # transcripts mirrored


BACKUP_TIP = "to configure backups"
DERIVE_HINT = "added `derive`"


def test_setup_hint_shows_until_backups_configured(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    db = str(tmp_path / "m.db")

    main(["--db", db, "stats"])  # never set up -> both tips appear on stderr
    err = capsys.readouterr().err
    assert BACKUP_TIP in err and DERIVE_HINT in err

    main(["--db", db, "stats", "--json"])  # machine-readable callers never see them
    assert "memware setup" not in capsys.readouterr().err

    main(["--db", db, "config", "backup.dest", str(tmp_path / "bk")])
    capsys.readouterr()
    main(["--db", db, "stats"])  # once a destination exists the backup tip is gone...
    err = capsys.readouterr().err
    assert BACKUP_TIP not in err
    assert DERIVE_HINT in err  # ...but nothing has asked about derive yet

    main(["--db", db, "config", "derive.auto", "false"])
    capsys.readouterr()
    main(["--db", db, "stats"])
    assert "memware setup" not in capsys.readouterr().err


def _answers(monkeypatch, *lines):
    """Feed setup's prompts; input() raises EOFError once they run out, like a closed stdin."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{x}\n" for x in lines)))


def _no_transcripts(db, tmp_path):
    """Point the backfill step at nothing, so setup never offers to index this machine's own
    ~/.claude/projects and the prompts are the same everywhere."""
    main(["--db", db, "config", "backup.transcript_src", str(tmp_path / "no-transcripts")])


def test_derive_consent_hint_after_an_upgrade_until_setup_asks(tmp_path, capsys, monkeypatch):
    """The reported install: setup last ran on 0.2.5, before derive existed, and derive.auto was
    never written. Every stats run says derive exists until setup has asked; setup asks, writes
    the answer, and the hint stops."""
    from memware.config import get_dotted, has_key, load_config

    db = str(tmp_path / "m.db")
    main(["--db", db, "config", "setup.completed_version", "0.2.5"])
    _no_transcripts(db, tmp_path)
    capsys.readouterr()

    main(["--db", db, "stats"])
    err = capsys.readouterr().err
    assert err.count(DERIVE_HINT) == 1
    assert "memware derive --plan" in err
    assert BACKUP_TIP not in err  # setup ran before; only the new question is raised

    main(["--db", db, "stats", "--json"])
    assert DERIVE_HINT not in capsys.readouterr().err

    _answers(monkeypatch, "", "n")  # keep no backup folder; decline derive
    assert main(["--db", db, "setup"]) == 0
    out = capsys.readouterr().out
    assert "Excerpts go to" in out and "memware derive --plan" in out
    assert "Enable automatic derive (runs at session start, at most once a day)? [y/N]" in out
    assert has_key("derive.auto")  # the decline is on file, not merely the default
    assert get_dotted(load_config(), "derive.auto") is False

    main(["--db", db, "stats"])
    assert "memware setup" not in capsys.readouterr().err


ANSWERED_OR_ASKED = [
    {"setup.completed_version": "0.2.5", "derive.auto": "false"},  # declined without setup
    {"setup.completed_version": "0.2.5", "derive.auto": "true"},  # switched on by hand
    {"setup.completed_version": "0.4.0"},  # setup already asked
    {"setup.completed_version": "0.10.0"},  # a string compare would call this older
]


@pytest.mark.parametrize("settings", ANSWERED_OR_ASKED)
def test_derive_consent_hint_stops_once_answered_or_asked(tmp_path, capsys, settings):
    db = str(tmp_path / "m.db")
    for k, v in settings.items():
        main(["--db", db, "config", k, v])
    capsys.readouterr()
    main(["--db", db, "stats"])
    assert DERIVE_HINT not in capsys.readouterr().err


def _notice(monkeypatch, capsys, db, source="startup"):
    """Run the plugin's session-start notice as Claude Code does: the hook payload on stdin."""
    payload = {"hook_event_name": "SessionStart", "source": source}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main(["--db", db, "notice", "--from-hook"]) == 0
    return capsys.readouterr()


def test_notice_tells_a_plugin_user_what_setup_has_not_asked(tmp_path, capsys, monkeypatch):
    """The reported install, seen from the plugin: setup last ran on 0.2.5 and derive.auto was
    never written. The session-start hook prints the hint `stats` prints as a systemMessage, and
    reads only the config: the store is never opened."""
    db = tmp_path / "never-opened.db"
    main(["--db", str(db), "config", "setup.completed_version", "0.2.5"])
    capsys.readouterr()

    got = _notice(monkeypatch, capsys, str(db))
    assert set(json.loads(got.out)) == {"systemMessage"}
    msg = json.loads(got.out)["systemMessage"]
    assert msg.count(DERIVE_HINT) == 1 and "memware derive --plan" in msg
    assert got.err == ""
    assert not db.exists()

    assert main(["--db", str(db), "notice"]) == 0  # by hand: the same line, plain
    assert capsys.readouterr().out == msg + "\n"
    assert main(["--db", str(db), "notice", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [msg]

    main(["--db", str(db), "stats"])  # one table behind both channels
    assert msg in capsys.readouterr().err


@pytest.mark.parametrize("settings", ANSWERED_OR_ASKED)
def test_notice_is_silent_once_answered_or_asked(tmp_path, capsys, monkeypatch, settings):
    db = str(tmp_path / "m.db")
    for k, v in settings.items():
        main(["--db", db, "config", k, v])
    capsys.readouterr()
    assert _notice(monkeypatch, capsys, db).out == ""
    assert main(["--db", db, "notice"]) == 0
    assert capsys.readouterr().out == ""


def test_notice_asks_with_no_config_but_not_with_a_broken_one(tmp_path, capsys, monkeypatch):
    """A plugin-only user who never ran a memware command has no config file: nobody has asked
    them. A config that will not parse may still hold an answer, so it prints nothing."""
    from memware.config import config_path

    db = str(tmp_path / "m.db")
    assert DERIVE_HINT in _notice(monkeypatch, capsys, db).out

    config_path().parent.mkdir(parents=True)
    for broken in ('{"derive": {"auto": false},', "[]", '"0.4.0"'):
        config_path().write_text(broken)
        got = _notice(monkeypatch, capsys, db)
        assert (got.out, got.err) == ("", "")


def test_notice_skips_a_compaction(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "m.db")
    main(["--db", db, "config", "setup.completed_version", "0.2.5"])
    capsys.readouterr()
    for source in ("startup", "resume", "clear"):
        got = _notice(monkeypatch, capsys, db, source)
        assert DERIVE_HINT in json.loads(got.out)["systemMessage"]
    assert _notice(monkeypatch, capsys, db, "compact").out == ""


@pytest.mark.parametrize("closed_stdin", [False, True], ids=["--yes", "closed-stdin"])
def test_setup_never_switches_derive_without_an_answer(tmp_path, capsys, monkeypatch, closed_stdin):
    from memware.config import get_dotted, has_key, load_config

    db = str(tmp_path / "m.db")
    _no_transcripts(db, tmp_path)
    args = ["--db", db, "setup", *([] if closed_stdin else ["--yes"])]

    _answers(monkeypatch)
    assert main(args) == 0
    assert "Automatic derive stays off" in capsys.readouterr().out
    assert not has_key("derive.auto")  # not even a decline is invented
    assert get_dotted(load_config(), "derive.auto") is False

    main(["--db", db, "config", "derive.auto", "true"])
    _answers(monkeypatch)
    assert main(args) == 0
    assert "Automatic derive stays on" in capsys.readouterr().out
    assert get_dotted(load_config(), "derive.auto") is True  # nor is a yes taken away


def test_setup_derive_answer_is_persisted_and_offered_back(tmp_path, capsys, monkeypatch):
    from memware.config import get_dotted, load_config

    db = str(tmp_path / "m.db")
    _no_transcripts(db, tmp_path)
    capsys.readouterr()

    _answers(monkeypatch, "", "y")
    assert main(["--db", db, "setup"]) == 0
    assert "Automatic derive on" in capsys.readouterr().out
    assert get_dotted(load_config(), "derive.auto") is True

    _answers(monkeypatch, "", "")  # re-run: Enter keeps what is on file
    assert main(["--db", db, "setup"]) == 0
    out = capsys.readouterr().out
    assert "Current: on" in out and "at most once a day)? [Y/n]" in out
    assert get_dotted(load_config(), "derive.auto") is True


def test_derive_destination_names_where_excerpts_go(tmp_path, monkeypatch):
    from memware.cli import _derive_destination
    from memware.config import load_config

    for k in ("MEMWARE_DERIVE_PROVIDER", "MEMWARE_DERIVE_MODEL", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert _derive_destination(load_config()) == (
        "haiku via the Claude Code CLI (`claude -p`) on your own subscription"
    )
    monkeypatch.setenv("MEMWARE_DERIVE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "small-model")
    assert _derive_destination(load_config()) == (
        "small-model at llm.example.com, an OpenAI-compatible endpoint"
    )


@pytest.mark.parametrize("have_packaging", [True, False])
def test_consent_versions_compare_as_versions(monkeypatch, have_packaging):
    from memware.cli import _older

    if not have_packaging:
        monkeypatch.setitem(sys.modules, "packaging.version", None)  # the import now fails
    assert _older("0.2.5", "0.4.0")
    assert not _older("0.10.0", "0.4.0")  # as strings, "0.10.0" < "0.4.0"
    assert not _older("0.4.0", "0.4.0")
    assert not _older("0.4", "0.4.0")
    assert not _older("0.4.1", "0.4.0")
    assert _older("not a version", "0.4.0")  # unreadable reads as never asked


def test_config_set_writes_only_that_key(tmp_path, capsys):
    """Saving the merged view wrote every default into the file as if chosen, so a user who
    had never been asked about derive read the same as one who had declined it."""
    from memware.config import config_path, has_key

    db = str(tmp_path / "m.db")
    main(["--db", db, "config", "backup.dest", str(tmp_path / "bk")])
    assert json.loads(config_path().read_text()) == {"backup": {"dest": str(tmp_path / "bk")}}
    assert not has_key("derive.auto")
    capsys.readouterr()

    main(["--db", db, "config", "--json"])  # reading still shows the merged defaults
    shown = json.loads(capsys.readouterr().out)
    assert shown["derive"]["auto"] is False and shown["backup"]["keep_days"] == [1, 3, 7, 14]


def test_bare_sync_catches_up_configured_transcript_src(tmp_path, capsys, monkeypatch):
    """`memware sync` with no path indexes the configured transcript source — what the
    SessionStart hook runs to catch up sessions whose SessionEnd never fired (e.g. a worktree
    force-killed by Orca). A path or --from-hook still targets exactly what's given."""
    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "home"))
    projects = tmp_path / "projects"
    (projects / "p").mkdir(parents=True)
    write_claude_jsonl(
        projects / "p" / "s.jsonl",
        "s",
        [("assistant", "2026-08-25T00:00:00Z", "the indexer catches up on the next session start")],
    )
    db = str(tmp_path / "m.db")
    main(["--db", db, "config", "backup.transcript_src", str(projects)])
    capsys.readouterr()
    assert main(["--db", db, "sync", "--json"]) == 0  # no path -> configured source
    assert json.loads(capsys.readouterr().out)["added"] == 1


def test_plain_output_is_tab_separated_id_first(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    main(["--db", db, "assert", "api", "listens on port", "8443"])
    capsys.readouterr()
    main(["--db", db, "beliefs", "--plain"])
    cols = capsys.readouterr().out.strip().splitlines()[0].split("\t")
    assert cols[0] == "1" and cols[1] == "api" and cols[3] == "8443"


def test_default_output_is_labeled_for_screen_readers(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    main(["--db", db, "assert", "api", "listens on port", "8443"])
    capsys.readouterr()
    main(["--db", db, "beliefs", "api"])
    out = capsys.readouterr().out
    assert "subject : api" in out and "value : 8443" in out


def test_assert_batch_from_stdin(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "m.db")
    monkeypatch.setattr(
        sys, "stdin", io.StringIO("api\tlistens on port\t8443\n# a comment\n\ndb\turl\tpg://x\n")
    )
    assert main(["--db", db, "assert", "-", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["asserted"] == 2


def test_assert_without_value_errors_cleanly(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    assert main(["--db", db, "assert", "api", "listens on port"]) == 2
    assert "SUBJECT RELATION VALUE" in capsys.readouterr().err


def test_completions_command(capsys):
    """With shtab installed it prints a script; without it, a clear install hint and exit 2.
    The test tolerates both so it passes whether or not the optional `[shell]` extra is present
    (e.g. the release job verifies against the bare wheel)."""
    try:
        import shtab  # noqa: F401

        has_shtab = True
    except ImportError:
        has_shtab = False
    rc = main(["completions", "zsh"])
    out = capsys.readouterr()
    if has_shtab:
        assert rc == 0 and "#compdef memware" in out.out
    else:
        assert rc == 2 and "shtab" in out.err


def test_help_shows_examples(capsys):
    import contextlib

    with contextlib.suppress(SystemExit):
        main(["recall", "--help"])
    out = capsys.readouterr().out
    assert "Examples:" in out and "memware recall" in out


def test_memware_home_override_legacy_and_xdg(tmp_path, monkeypatch):
    from memware.config import memware_home

    monkeypatch.setenv("MEMWARE_HOME", str(tmp_path / "explicit"))
    assert memware_home() == tmp_path / "explicit"  # explicit override wins

    monkeypatch.delenv("MEMWARE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert memware_home() == tmp_path / "xdg" / "memware"  # fresh install -> XDG

    (tmp_path / "h" / ".memware").mkdir(parents=True)
    assert memware_home() == tmp_path / "h" / ".memware"  # existing legacy dir wins over XDG
