"""``memware prune --apply`` redacts a secret from every belief that holds it (:func:`redact`), and
refuses a redaction too broad to be a secret's (:func:`redaction_refusal`). A copy the redaction
misses stays in the ledger and is injected into later prompts, while the prune reports it gone.

Matching stays case-sensitive (a secret is), but it is not fooled by the forms one secret takes in
text: composed or decomposed Unicode (NFC, NFD, NFKC), percent-encoding in a URL or connection
string, or an invisible character (zero-width space, soft hyphen, bidi mark) inside it. The refusal
measures the text the redaction will actually match. See docs/security.md."""

import random
import unicodedata
from urllib.parse import quote, unquote

from memware.ledger import (
    REDACT_MAX_BELIEFS,
    REDACTED,
    Redaction,
    assert_belief,
    redact,
    redaction_refusal,
)

INVISIBLE = ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u200e", "\u202c"]


def _redact(store, text, **kw):
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        return redact(store, text, apply=True, **kw)
    finally:
        store.conn.execute("COMMIT")


def _plan(store, text, **kw) -> Redaction:
    return redact(store, text, **kw)


def _text(store, belief_id: int) -> str:
    row = store.conn.execute(
        "SELECT subject, relation, value, coalesce(source, '') FROM belief WHERE id=?", (belief_id,)
    ).fetchone()
    return " | ".join(row)


def _value(store, belief_id: int) -> str:
    return store.conn.execute("SELECT value FROM belief WHERE id=?", (belief_id,)).fetchone()[0]


def _canonical(s: str) -> str:
    """What a reader sees: invisible characters dropped, composed, percent-decoded."""
    s = "".join(c for c in s if unicodedata.category(c) != "Cf" and c != "\u00ad")
    return unicodedata.normalize("NFC", unquote(s))


def test_a_decomposed_copy_is_redacted(store):
    secret = unicodedata.normalize("NFC", "pässwörd-42")
    b = assert_belief(
        store, "db", "password", "is " + unicodedata.normalize("NFD", secret)
    ).belief_id
    c = assert_belief(store, "db2", "password", "is " + secret).belief_id
    _redact(store, unicodedata.normalize("NFD", secret))
    for bid in (b, c):
        assert secret not in _canonical(_text(store, bid)), _text(store, bid)
        assert REDACTED in _text(store, bid)


def test_a_percent_encoded_copy_is_redacted(store):
    secret = "p@ss:w0rd/x"
    b = assert_belief(
        store, "app database", "url", f"postgres://app:{quote(secret, safe='')}@db:5432/app"
    ).belief_id
    plan = _plan(store, secret)
    assert plan.beliefs == [b]
    _redact(store, secret)
    assert secret not in _canonical(_text(store, b))
    assert _value(store, b) == f"postgres://app:{REDACTED}@db:5432/app"


def test_a_copy_with_an_invisible_character_inside_is_redacted(store):
    secret = "sk-live-abc123def"
    b = assert_belief(store, "stripe", "key", "sk-live\u200b-abc123\u00addef").belief_id
    _redact(store, secret)
    assert "abc123" not in _text(store, b)


def test_matching_stays_case_sensitive(store):
    """A secret is case-sensitive, and derive copies values exactly; folding case would widen what
    a six-character text matches to words that only look like it."""
    assert_belief(store, "legacy", "password", "HUNTER2-XYZ")
    assert _plan(store, "hunter2-xyz").beliefs == []


def test_a_prefix_redaction_sees_the_same_forms(store):
    secret = unicodedata.normalize("NFC", "café-token-1")
    b = assert_belief(store, "svc", "token", unicodedata.normalize("NFD", secret) + " (rotated)")
    _redact(store, secret, prefix=True)
    assert _value(store, b.belief_id) == f"{REDACTED} (rotated)"


def test_the_refusal_measures_what_will_match(store):
    """Invisible padding or decomposed letters make a short text look long enough: six code points
    that match two visible characters in every belief holding them."""
    assert_belief(store, "notes", "about", "ab testing")
    for text in ("\u200bab\u200b\u200b\u200b", "\u00adab\u2060\u2060\u2060"):
        plan = _plan(store, text)
        assert plan.rewrites == 1, text.encode("unicode_escape")
        assert redaction_refusal(plan, text) is not None
    assert_belief(store, "cafe", "motto", "ééé!")
    decomposed = unicodedata.normalize("NFD", "ééé!")
    assert len(decomposed) == 7
    plan = _plan(store, decomposed)
    assert plan.rewrites == 1
    assert redaction_refusal(plan, decomposed) is not None


def test_the_refusal_still_admits_a_real_secret(store):
    assert_belief(store, "api", "key", "tok-9f8e7d6c")
    plan = _plan(store, "tok-9f8e7d6c")
    assert plan.rewrites == 1 and redaction_refusal(plan, "tok-9f8e7d6c") is None
    broad = Redaction(list(range(REDACT_MAX_BELIEFS + 1)), [], [])
    assert redaction_refusal(broad, "tok-9f8e7d6c") is not None


def _disguise(rng: random.Random, secret: str) -> str:
    form = rng.choice(["same", "nfd", "nfkc-wide", "invisible", "percent"])
    if form == "nfd":
        return unicodedata.normalize("NFD", secret)
    if form == "nfkc-wide":  # the fullwidth forms NFKC folds back to ASCII
        return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in secret)
    if form == "invisible":
        i = rng.randint(1, len(secret) - 1)
        return secret[:i] + rng.choice(INVISIBLE) + secret[i:]
    if form == "percent":
        return quote(secret, safe="")
    return secret


def test_fuzzed_disguises_never_survive_and_nothing_else_changes(store):
    rng = random.Random(4242)
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789-_@:/+=.!éöñçå"
    words = ["the", "token", "is", "set", "in", "config", "for", "prod", "rotated", "value"]
    for round_ in range(150):
        secret = "".join(rng.choice(alphabet) for _ in range(rng.randint(8, 20)))
        disguised = _disguise(rng, secret)
        where = rng.choice(["subject", "relation", "value"])
        fields = {f: " ".join(rng.sample(words, 3)) for f in ("subject", "relation", "value")}
        fields[where] = f"{rng.choice(words)} {disguised} {rng.choice(words)}"
        fields["subject"] = f"{fields['subject']} {round_}"  # a key of its own
        target = assert_belief(store, fields["subject"], fields["relation"], fields["value"])
        bystander = assert_belief(store, f"bystander {round_}", "holds", "nothing secret here")
        before = _text(store, bystander.belief_id)
        plan = _plan(store, secret)
        assert target.belief_id in plan.beliefs, (secret, disguised)
        _redact(store, secret)
        after = _canonical(_text(store, target.belief_id))
        assert unicodedata.normalize("NFKC", secret) not in unicodedata.normalize("NFKC", after)
        assert _text(store, bystander.belief_id) == before
