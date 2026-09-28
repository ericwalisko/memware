"""Instruction-shaped text: a belief that reads as an order to the agent rather than a fact.

A belief is injected into later prompts without anyone asking for it, so a stored string that
reads as an order would come back in every matching session as one. Text a user pasted once (a
README, an issue, a web page), or a tool result an agent read, can carry such an order.
:func:`instruction_shaped` is the deterministic test, with no model, that
derive applies before filing a triple and the injection gate applies to every belief, whoever
wrote it (:class:`memware.volatile.Gate`). A belief it matches stays in the ledger, in recall and
in ``memware beliefs``, marked; it is never injected unasked.

It looks for what prompt injection needs and a durable fact does not: a demand to drop earlier
instructions, an order addressed to the agent, a standing order ("from now on"), an action hidden
from the user, a download piped into a shell, a role or prompt marker, and a value that opens with
an order. Text is compared after NFKC and with invisible characters removed, so fullwidth letters
or a zero-width space inside a word do not hide it. See docs/security.md.
"""

from __future__ import annotations

import re
import unicodedata

_ORDER_VERBS = (
    r"run|execute|use|call|send|include|add|append|prepend|start|begin|reply|respond|answer|say|"
    r"tell|ask|push|commit|merge|delete|remove|install|download|fetch|curl|wget|trust|ignore|"
    r"follow|obey|output|print|write|read|open|visit|click|disable|enable|skip|approve|accept|"
    r"grant|share|upload|post|copy|forward|reveal|mention|show|inform|notify"
)

_ANYWHERE: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (why, re.compile(pattern))
    for why, pattern in (
        (
            "asks to drop earlier instructions",
            r"\b(?:ignore|disregard|forget|override|bypass)\b"
            r"(?:\s+(?:all|any|every|the|your|my|of|these|those|previous|prior|above|earlier|"
            r"preceding|existing|former|original|other|system|safety|security)){0,8}"
            r"\s+(?:instructions?|prompts?|rules|guidelines|guardrails|directions|directives|"
            r"context|constraints|policies|messages)\b",
        ),
        (
            "an order addressed to the agent",
            r"\b(?:you|claude|(?:the\s+)?(?:assistant|agent|ai|model|llm))\s+(?:must|should|shall|"
            r"needs? to|ha(?:ve|s) to|(?:are|is) required to|are to|may not|cannot|can't|"
            r"(?:are|is) not allowed to|will (?:now|always|never))\b",
        ),
        (
            "a standing order",
            r"\bfrom now on\b|\byou are now\b|\bnew (?:system )?instructions?\b"
            r"|\b(?:developer|god|jailbreak|dan) mode\b",
        ),
        (
            "hides an action from the user",
            r"\bwithout (?:asking|telling|informing|notifying|alerting|confirming|consulting|"
            r"checking with)\b|\b(?:do not|don't|never) (?:tell|inform|notify|alert|mention|reveal|"
            r"show|ask)\b|\b(?:silently|secretly|quietly|covertly) (?:" + _ORDER_VERBS + r")\b",
        ),
        (
            "pipes a download into a shell",
            r"\b(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|;&]{0,300}\|\s*(?:sudo\s+)?"
            r"(?:(?:ba|z|k|c|tc|da|fi)?sh|python[0-9.]*|perl|ruby|node|php|iex|invoke-expression)\b"
            r"|\bbase64\s+(?:-d|--decode|-D)\b[^|;&]{0,300}\|\s*(?:sudo\s+)?(?:ba|z|k|c|tc|da)?sh\b"
            r"|\b(?:ba|z)?sh\s+-c\s+[\"']?\$\(\s*(?:curl|wget)\b",
        ),
        (
            "deletes a home or root directory",
            r"\brm\s+-[a-z]*r[a-z]*\s+(?:--no-preserve-root\s+)?(?:~/?|/\*?|\$home/?|\*)(?:\s|$)",
        ),
        (
            "a role or prompt marker",
            r"(?:^|[.!?]\s)(?:system|assistant|human|developer)\s*:"
            r"|<\s*/?\s*(?:system|assistant|human|developer|instructions?|im_start|im_end|"
            r"function_calls|antml:[a-z_]+)\b[^>]{0,300}>"
            r"|\[/?(?:inst|sys)\]|<<\s*/?\s*sys\s*>>|<\|[a-z_]+\|>"
            r"|\b(?:note|message|instructions?|reminder)\s+(?:to|for)\s+(?:the\s+)?"
            r"(?:ai|assistant|agent|model|llm|claude|chatgpt|gpt)\b",
        ),
    )
)

_ROLE_OPENS = re.compile(r"^(?:system|assistant|human|developer)\s*:")
_ORDER_OPENS = re.compile(
    r"^(?:please|kindly|make sure|be sure|remember to|ensure (?:that )?you"
    r"|(?:important|urgent|attention|critical)\s*[:!]"
    r"|(?:always|never|do not|don't) (?:" + _ORDER_VERBS + r")\b)"
)


def _visible(text: str) -> str:
    """NFKC, invisible (format) characters removed, lower case, whitespace collapsed."""
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(c for c in folded if unicodedata.category(c) != "Cf")
    return " ".join(folded.casefold().split())


def instruction_shaped(subject: str, relation: str, value: str) -> str | None:
    """Why the triple reads as an order to the agent, or None for a fact."""
    whole = _visible(f"{subject} {relation} {value}")
    for why, pattern in _ANYWHERE:
        if pattern.search(whole):
            return why
    fields = [_visible(f) for f in (subject, relation, value)]
    if any(_ROLE_OPENS.match(f) for f in fields):
        return "a role or prompt marker"
    if any(_ORDER_OPENS.match(f) for f in fields[1:]):
        return "opens with an order"
    return None


def one_line(text: str) -> str:
    """``text`` as one line of plain text: control and line-separator characters become spaces,
    invisible (format) characters go, and whitespace runs close up. A value cannot start a line or a
    turn of its own in an injected block, nor hide words a person reading ``memware beliefs`` would
    not see. Text that has none of these, as derive writes it, comes back unchanged."""
    if text.isprintable() and "  " not in text and text == text.strip():
        return text
    out = []
    for c in text:
        cat = unicodedata.category(c)
        if cat == "Cf":
            continue
        out.append(" " if cat in ("Cc", "Zl", "Zp") else c)
    return " ".join("".join(out).split())
