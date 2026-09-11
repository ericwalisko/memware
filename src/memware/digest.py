"""``memware digest``: what memware holds for the project a session opens in.

The prompt hook injects beliefs only, and nothing at all until the ledger fills, so for most
sessions this block is the first memware content a model sees: one line naming recall, the
project's most recent sessions (date and first prompt), and the currently valid beliefs whose
subject names the project. A model that has seen memware content once is far likelier to call
recall later.

Sessions are scoped by transcript path. Claude Code keeps a project's transcripts in
``<config dir>/projects/<cwd, every non-alphanumeric character a "-">/`` and ingest stores each
turn's resolved file path as ``turn.source``, so a project is a range of ``source`` values that
the ``UNIQUE(source, seq)`` index answers: no FTS over turns, no table scan. A git worktree
counts as its repository, so the primary checkout and every live worktree are one project.
Other harnesses' transcripts have no such layout and never appear.

Nothing here writes. The digest is unsolicited, so it records no use: ``use_count`` means a
deliberate retrieval.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from memware.config import get_dotted, load_config
from memware.index import _subject_terms, fts_query
from memware.store import Store
from memware.term import ellipsis

MAX_DIR_NAME = 200
"""Claude Code cuts a longer project directory name here and appends a hash of the path."""

PROMPT_CHARS = 120
DEFAULT_MAX_CHARS = 1200


def _js_string_hash(text: str) -> int:
    """``h = (h << 5) - h + charCodeAt(i) | 0`` over UTF-16 code units: a signed 32-bit int."""
    h = 0
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        h = (h * 31 + int.from_bytes(units[i : i + 2], "little")) & 0xFFFFFFFF
    return h - (1 << 32) if h & 0x80000000 else h


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def project_dir_name(cwd: str) -> str:
    """The directory name Claude Code keeps ``cwd``'s transcripts under, as Claude Code 2.1
    computes it. JavaScript's regex sees UTF-16 code units, so a character outside the BMP
    becomes two dashes."""
    name = re.sub(r"[^A-Za-z0-9]", lambda m: "--" if ord(m.group()) > 0xFFFF else "-", cwd)
    if len(name) <= MAX_DIR_NAME:
        return name
    return f"{name[:MAX_DIR_NAME]}-{_base36(abs(_js_string_hash(cwd)))}"


def _unique(paths: Iterable[Path]) -> list[Path]:
    return list(dict.fromkeys(paths))


def _common_dir(dot_git: Path) -> Path | None:
    """The repository's shared git directory, from a checkout's ``.git`` directory or the
    ``gitdir:`` file a linked worktree has in its place."""
    if dot_git.is_dir():
        return dot_git
    try:
        head = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("gitdir:"):
        return None
    gitdir = Path(head.removeprefix("gitdir:").strip())
    if not gitdir.is_absolute():
        gitdir = dot_git.parent / gitdir
    try:
        common = Path((gitdir / "commondir").read_text(encoding="utf-8").strip())
    except OSError:
        return gitdir.resolve()  # a submodule: its git directory is not shared
    return (common if common.is_absolute() else gitdir / common).resolve()


@dataclass(frozen=True)
class Project:
    root: Path
    """The checkout holding the cwd, or the cwd itself outside a git repository."""
    checkouts: tuple[Path, ...]
    """Every directory whose sessions belong to the project."""
    names: tuple[str, ...]
    """Directory and package names a belief's subject can name the project by."""


def _package_names(root: Path) -> list[str]:
    names: list[str] = []
    try:
        import tomllib

        with (root / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
        name = data.get("project", {}).get("name") or data.get("tool", {}).get("poetry", {}).get(
            "name"
        )
        if isinstance(name, str):
            names.append(name)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        name = json.loads((root / "package.json").read_text(encoding="utf-8")).get("name")
        if isinstance(name, str):
            names.append(name)
    except (OSError, ValueError, AttributeError):
        pass
    return names


def resolve_project(cwd: Path) -> Project:
    """The project at ``cwd``. Inside a git repository that is the whole repository: the checkout
    holding ``cwd``, the primary checkout and every live linked worktree, read from git's own
    files with no git process. A removed worktree's sessions stay reachable through recall only.
    """
    cwd = Path(os.path.abspath(cwd))
    for root in (cwd, *cwd.parents):
        if (root / ".git").exists():
            break
    else:
        return Project(cwd, (cwd,), tuple(dict.fromkeys([cwd.name, *_package_names(cwd)])))
    checkouts = [cwd, root]
    names = [root.name]
    common = _common_dir(root / ".git")
    if common is not None:
        if common.name == ".git":
            checkouts.append(common.parent)
            names.append(common.parent.name)
        for gitdir in sorted((common / "worktrees").glob("*/gitdir")):
            try:
                checkouts.append(Path(gitdir.read_text(encoding="utf-8").strip()).parent)
            except OSError:
                continue
    names += _package_names(root)
    return Project(root, tuple(_unique(checkouts)), tuple(dict.fromkeys(n for n in names if n)))


def transcript_dirs(project: Project, transcript_path: str | None = None) -> list[Path]:
    """Every directory Claude Code could have written the project's transcripts to: each
    checkout, as given and resolved, under ``$CLAUDE_CONFIG_DIR/projects``,
    ``~/.claude/projects`` and the configured ``backup.transcript_src``. The hook's own
    ``transcript_path`` names this session's directory exactly and is included as is."""
    roots: list[Path] = []
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        roots.append(Path(config_dir).expanduser() / "projects")
    roots.append(Path("~/.claude/projects").expanduser())
    src = get_dotted(load_config(), "backup.transcript_src")
    if src:
        roots.append(Path(str(src)).expanduser())
    cwds = _unique(p for c in project.checkouts for p in (c, c.resolve()))
    dirs = [r.resolve() / project_dir_name(str(c)) for r in _unique(roots) for c in cwds]
    if transcript_path:
        dirs.insert(0, Path(transcript_path).expanduser().resolve().parent)
    return _unique(dirs)


def _source_ranges(dirs: Iterable[Path]) -> list[tuple[str, str]]:
    """``[lo, hi)`` on ``turn.source`` for every file under each directory. ``hi`` is the
    directory followed by the character after the separator, so a sibling whose name only
    begins the same way (``app`` and ``app-old``) falls outside."""
    after_sep = chr(ord(os.sep) + 1)
    return list(dict.fromkeys((f"{d}{os.sep}", f"{d}{after_sep}") for d in dirs))


def sessions_sql(ranges: int) -> str:
    """One index range per directory; every session under them with its latest turn time."""
    where = " OR ".join(["(source >= ? AND source < ?)"] * ranges)
    return (
        f"SELECT session, max(ts) AS last FROM turn WHERE {where} "
        "GROUP BY session ORDER BY last IS NULL, last DESC"
    )


FIRST_PROMPT_SQL = (  # sorts ids, not texts: a long session's turns never pass through the sorter
    "SELECT text FROM turn WHERE id = (SELECT id FROM turn WHERE session = ? "
    "ORDER BY role != 'user', ts IS NULL, ts, id LIMIT 1)"
)


def _clip(text: str, width: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    mark = ellipsis()
    return flat[: width - len(mark)].rstrip() + mark


def _plural(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def project_beliefs(conn: sqlite3.Connection, names: Iterable[str]) -> list[sqlite3.Row]:
    """Currently valid beliefs whose subject shares a whole term with one of ``names``, newest
    first: the same subject test the prompt hook applies (``require_subject``)."""
    q = fts_query(" ".join(names))
    if not q:
        return []
    terms = {t.strip('"') for t in q.split(" OR ")}
    try:
        rows = conn.execute(
            "SELECT b.id, b.subject, b.relation, b.value, b.valid_from FROM belief_fts "
            "JOIN belief b ON b.id = belief_fts.rowid "
            "WHERE belief_fts MATCH ? AND b.valid_to IS NULL AND b.status = 'committed' "
            "ORDER BY b.valid_from DESC, b.id DESC",
            (f"subject : ({q})",),
        ).fetchall()
    except sqlite3.OperationalError:  # a name FTS5 cannot parse: no beliefs, not a failure
        return []
    return [r for r in rows if _subject_terms(r["subject"]) & terms]


def digest(
    store: Store,
    cwd: Path,
    *,
    k: int = 5,
    max_chars: int = DEFAULT_MAX_CHARS,
    session: str | None = None,
    transcript_path: str | None = None,
) -> str:
    """The block for ``cwd``'s project, or ``""`` when memware has no session for it.

    ``session`` (the one starting) is left out of the list and the count. The opening line
    always prints; session and belief lines follow while the block stays within ``max_chars``.
    """
    project = resolve_project(cwd)
    ranges = _source_ranges(transcript_dirs(project, transcript_path))
    rows = store.conn.execute(
        sessions_sql(len(ranges)), [bound for pair in ranges for bound in pair]
    ).fetchall()
    rows = [r for r in rows if r["session"] != session]
    if not rows:
        return ""
    beliefs = project_beliefs(store.conn, project.names)

    session_lines: list[str] = []
    for r in rows[: max(0, k)]:
        first = store.conn.execute(FIRST_PROMPT_SQL, (r["session"],)).fetchone()
        when = (r["last"] or "")[:10] or "undated"
        session_lines.append(f"- {when} {_clip(first['text'] if first else '', PROMPT_CHARS)}")
    belief_lines: list[str] = []
    for b in beliefs:
        since = f" (since {b['valid_from'][:10]})" if b["valid_from"] else ""
        belief_lines.append(f"- {b['subject']} {b['relation']}: {b['value']}{since}")

    out = (
        f"memware has {_plural(len(rows), 'session')} and {_plural(len(beliefs), 'belief')} "
        "for this project; call recall for past decisions, earlier sessions, anything not in "
        "the working tree."
    )
    for title, lines in (
        ("Recent sessions here (last active, first prompt):", session_lines),
        ("Current beliefs about this project:", belief_lines),
    ):
        section = ""
        for line in lines:
            piece = ("" if section else f"\n{title}") + f"\n{line}"
            if len(out) + len(section) + len(piece) > max_chars:
                break
            section += piece
        out += section
    return out
