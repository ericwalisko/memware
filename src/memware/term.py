"""Output policy for terminals, tuned for accessibility and scripting.

Two principles:

* **No information is ever carried by colour.** memware emits no colour escapes at
  all, so it is legible in any palette, to colour-blind users, and under ``NO_COLOR``
  by construction — there is nothing to disable.
* **ASCII on request.** A screen reader or a non-UTF-8 terminal can mispronounce or
  mangle glyphs like ``…``. ASCII mode swaps them for plain equivalents (``...``).
  It turns on when ``MEMWARE_ASCII`` is set to a truthy value, or when the locale is
  not UTF-8 (so legacy terminals degrade gracefully without any flag).
"""

from __future__ import annotations

import os

_FALSEY = {"", "0", "false", "no", "off"}


def ascii_mode() -> bool:
    """True when output should avoid non-ASCII glyphs."""
    val = os.environ.get("MEMWARE_ASCII")
    if val is not None:
        return val.lower() not in _FALSEY
    loc = os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG") or ""
    if not loc:
        return False  # no locale info: assume a modern UTF-8 terminal
    low = loc.lower()
    return "utf-8" not in low and "utf8" not in low


def ellipsis() -> str:
    """The elision marker: ``...`` in ASCII mode, ``…`` otherwise."""
    return "..." if ascii_mode() else "…"
