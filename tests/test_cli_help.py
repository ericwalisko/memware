"""`--help` for the top level and every subcommand is recorded in ``tests/data/cli_help.txt``
(``cli_help_py313.txt`` on Python 3.13+, whose argparse aligns the command column differently).

The split of ``memware.cli`` into one module per subcommand must not change a byte of what a
user reads, so this pins the whole help surface. After an intentional wording change,
regenerate the fixture with ``python tests/test_cli_help.py`` under each Python family."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURE = (
    Path(__file__).parent
    / "data"
    / ("cli_help_py313.txt" if sys.version_info >= (3, 13) else "cli_help.txt")
)
_DB = "/fixture/memware.db"  # the --db default is printed in every help; pin it
_COLUMNS = "80"


def _walk(
    parser: argparse.ArgumentParser, path: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _walk(sub, (*path, name))


def _recorded() -> str:
    return FIXTURE.read_bytes().decode("utf-8").replace("\r\n", "\n")  # a Windows checkout


def render_all() -> str:
    """Every ``memware [COMMAND ...] --help`` text, in parser order, under a marker line."""
    saved = {k: os.environ.get(k) for k in ("MEMWARE_DB", "COLUMNS")}
    os.environ["MEMWARE_DB"] = _DB
    os.environ["COLUMNS"] = _COLUMNS
    try:
        from memware.cli import build_parser

        out = []
        for path, parser in _walk(build_parser(), ()):
            out.append(f"===== memware {' '.join(path)} --help =====\n")
            out.append(parser.format_help())
        return "".join(out)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_help_output_is_unchanged() -> None:
    assert render_all() == _recorded()


def test_main_help_flag_matches_fixture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`memware --help` through the entry point prints the first section of the fixture."""
    monkeypatch.setenv("MEMWARE_DB", _DB)
    monkeypatch.setenv("COLUMNS", _COLUMNS)
    from memware.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    top = _recorded().split("===== memware ", 2)[1]
    assert capsys.readouterr().out == top.split("\n", 1)[1]


if __name__ == "__main__":
    FIXTURE.write_bytes(render_all().encode("utf-8"))
    sys.stdout.write(f"wrote {FIXTURE}\n")
