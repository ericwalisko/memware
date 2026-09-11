import json
import threading
import urllib.error
import urllib.request

import pytest

from gateway import auth, server
from gateway.auth import KeyRing, rotate_keys, sign, verify
from gateway.config import RETRY_LIMIT, Settings

NOW = 1_700_000_000


def ring() -> KeyRing:
    return KeyRing(active_id="k1", keys={"k1": b"secret-one"}, grace_seconds=60)


def signed_headers(secret: bytes, key_id: str, body: bytes, ts: int = NOW) -> dict[str, str]:
    return {
        auth.HEADER_KEY_ID: key_id,
        auth.HEADER_TIMESTAMP: str(ts),
        auth.HEADER_SIGNATURE: sign(secret, "POST", "/v1/jobs", ts, body),
    }


def test_sign_and_verify_roundtrip() -> None:
    body = b'{"job": "resize"}'
    headers = signed_headers(b"secret-one", "k1", body)
    assert verify(ring(), "POST", "/v1/jobs", headers, body, now=NOW) == "k1"


def test_tampered_body_fails_signature() -> None:
    headers = signed_headers(b"secret-one", "k1", b'{"job": "resize"}')
    with pytest.raises(auth.AuthError, match="signature mismatch"):
        verify(ring(), "POST", "/v1/jobs", headers, b'{"job": "delete"}', now=NOW)


def test_stale_timestamp_is_rejected() -> None:
    headers = signed_headers(b"secret-one", "k1", b"", ts=NOW - 1000)
    with pytest.raises(auth.AuthError, match="skew"):
        verify(ring(), "POST", "/v1/jobs", headers, b"", max_skew_seconds=300, now=NOW)


def test_rotate_keys_keeps_old_key_inside_grace_window() -> None:
    r = rotate_keys(ring(), "k2", b"secret-two", now=NOW)
    assert r.active_id == "k2"
    old = signed_headers(b"secret-one", "k1", b"", ts=NOW + 30)
    assert verify(r, "POST", "/v1/jobs", old, b"", now=NOW + 30) == "k1"
    with pytest.raises(auth.AuthError, match="expired"):
        verify(r, "POST", "/v1/jobs", old, b"", now=NOW + 61)


def test_forward_gives_up_after_retry_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def refuse(req: urllib.request.Request, timeout: float | None = None) -> None:
        calls.append(req.full_url)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(server.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    with pytest.raises(server.UpstreamError):
        server.forward(Settings(), "POST", "/v1/jobs", b"{}")
    assert len(calls) == RETRY_LIMIT


def test_healthz_needs_no_signature() -> None:
    httpd = server.serve(Settings(host="127.0.0.1", port=0), ring())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            assert json.loads(resp.read())["ok"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
