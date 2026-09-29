"""`memware config`."""

from __future__ import annotations

import argparse
import sys

from memware import relevance
from memware.cli._common import AddCommand, _out
from memware.volatile import WINDOW_KEY, parse_days


def _relevance_notice(mode: str) -> None:
    """What switching the relevance filter on sends, and where, before the first prompt does."""
    from memware.derive import env_file

    print(
        f"relevance.mode {mode}: from the next prompt, the prompt hook and the Hermes provider "
        f"send each prompt and its candidate facts to TypeSafe ({relevance.ENDPOINT}); "
        "`memware config relevance.mode off` stops it",
        file=sys.stderr,
    )
    if relevance.api_key() is None:
        print(
            f"no {relevance.KEY} in the environment or {env_file()}: until one is set, nothing "
            "is sent and injection is unchanged",
            file=sys.stderr,
        )


def cmd_config(a: argparse.Namespace) -> int:
    from memware.config import (
        config_path,
        get_dotted,
        load_config,
        load_user_config,
        save_config,
        set_dotted,
    )

    if a.key and a.value is not None:
        val: object = a.value
        if a.key.endswith("keep_days"):
            val = [int(x) for x in a.value.replace(",", " ").split()]
        elif a.key == WINDOW_KEY:
            days = parse_days(a.value)
            if days is None:
                print(
                    f"{WINDOW_KEY} takes a number of days, 0 or more (0 never injects a volatile "
                    f"belief); got {a.value!r}, nothing written",
                    file=sys.stderr,
                )
                return 2
            val = int(days) if days.is_integer() else days
        elif a.key.startswith("relevance."):
            parsed = relevance.parse_setting(a.key, a.value)
            name = a.key.removeprefix("relevance.")
            if parsed is None:
                expects = relevance.EXPECTS.get(name)
                print(
                    f"{a.key} takes {expects}"
                    if expects
                    else f"{a.key} is not a setting; relevance takes {', '.join(relevance.EXPECTS)}",
                    f"; got {a.value!r}, nothing written",
                    sep="",
                    file=sys.stderr,
                )
                return 2
            val = parsed
            if name == "mode" and val != "off":
                _relevance_notice(str(val))
        elif a.value.lower() in ("true", "false"):
            val = a.value.lower() == "true"
        user = load_user_config()  # write the one key; defaults stay defaults, not choices
        set_dotted(user, a.key, val)
        save_config(user)
        _out({a.key: get_dotted(user, a.key), "path": str(config_path())}, a.json)
    elif a.key:
        _out({a.key: get_dotted(load_config(), a.key)}, a.json)
    else:
        _out({**load_config(), "path": str(config_path())}, a.json)
    return 0


def register(add: AddCommand) -> None:
    s = add("config", "show or set configuration (e.g. backup.dest, backup.keep_days)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)
