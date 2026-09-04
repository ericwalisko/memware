import memware.term as term


def test_ascii_mode_follows_env_and_locale(monkeypatch):
    monkeypatch.delenv("MEMWARE_ASCII", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert term.ascii_mode() is False
    assert term.ellipsis() == "…"

    monkeypatch.setenv("MEMWARE_ASCII", "1")  # explicit on
    assert term.ascii_mode() is True
    assert term.ellipsis() == "..."

    monkeypatch.setenv("MEMWARE_ASCII", "0")  # explicit off wins over a non-UTF-8 locale
    monkeypatch.setenv("LANG", "C")
    assert term.ascii_mode() is False

    monkeypatch.delenv("MEMWARE_ASCII", raising=False)  # no flag, non-UTF-8 locale -> ascii
    monkeypatch.setenv("LANG", "C")
    assert term.ascii_mode() is True
