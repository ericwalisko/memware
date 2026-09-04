import memware.term as term

_LOCALE_VARS = ("MEMWARE_ASCII", "LC_ALL", "LC_CTYPE", "LANG")


def test_ascii_mode_follows_env_and_locale(monkeypatch):
    for v in _LOCALE_VARS:  # start from a known-clean locale (CI machines vary)
        monkeypatch.delenv(v, raising=False)

    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert term.ascii_mode() is False
    assert term.ellipsis() == "…"

    monkeypatch.setenv("MEMWARE_ASCII", "1")  # explicit on
    assert term.ascii_mode() is True
    assert term.ellipsis() == "..."

    monkeypatch.setenv("MEMWARE_ASCII", "0")  # explicit off wins over a non-UTF-8 locale
    monkeypatch.setenv("LANG", "C")
    assert term.ascii_mode() is False

    # No flag + a non-UTF-8 locale -> ascii. Clear LC_ALL/LC_CTYPE so LANG decides
    # (POSIX precedence is LC_ALL > LC_CTYPE > LANG, which the code honours).
    monkeypatch.delenv("MEMWARE_ASCII", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.setenv("LANG", "C")
    assert term.ascii_mode() is True


def test_lc_all_takes_precedence_over_lang(monkeypatch):
    for v in _LOCALE_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("LANG", "C")  # would say ascii...
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")  # ...but LC_ALL wins
    assert term.ascii_mode() is False
