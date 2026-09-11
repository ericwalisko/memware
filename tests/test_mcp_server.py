"""An MCP tool fires only when the model elects to call it, and the description is all it has to
decide with. recall competes with grep, which is in context, faster, and usually right, so its
description leads with when to call it rather than how. Descriptions are read on every turn, so
these tests also hold recall to a word budget: they erode one helpful sentence at a time."""

import asyncio

import pytest

from memware.mcp_server import build

pytest.importorskip("mcp")


def _descriptions() -> dict[str, str]:
    return {t.name: t.description or "" for t in asyncio.run(build().list_tools())}


def test_recall_leads_with_when_to_call_it():
    d = _descriptions()["recall"]
    assert d.startswith("Call this when"), d
    assert "Not for:" in d, d


def test_recall_description_stays_under_budget():
    words = len(_descriptions()["recall"].split())
    assert words < 130, words


def test_beliefs_and_read_session_say_when_to_call_them():
    d = _descriptions()
    for name in ("beliefs", "read_session"):
        assert d[name].startswith("Call this"), d[name]
