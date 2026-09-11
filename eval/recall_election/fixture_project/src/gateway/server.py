"""HTTP front door: verifies request signatures and forwards jobs to the runner."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gateway import __version__
from gateway.auth import AuthError, KeyRing, verify
from gateway.config import (
    KEY_GRACE_SECONDS,
    RETRY_BACKOFF_SECONDS,
    RETRY_LIMIT,
    Settings,
    load_settings,
)

log = logging.getLogger("gateway")


class UpstreamError(Exception):
    """The runner could not be reached after RETRY_LIMIT attempts."""


def forward(settings: Settings, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
    """Send one request to the runner, retrying on 5xx and connection errors."""
    url = settings.upstream_url.rstrip("/") + path
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=settings.upstream_timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return e.code, e.read()
            last = f"upstream returned {e.code}"
        except (urllib.error.URLError, TimeoutError) as e:
            last = f"upstream unreachable: {e}"
        if attempt >= RETRY_LIMIT:
            raise UpstreamError(last)
        log.warning("%s (attempt %d/%d)", last, attempt, RETRY_LIMIT)
        time.sleep(RETRY_BACKOFF_SECONDS * attempt)


class Handler(BaseHTTPRequestHandler):
    settings: Settings
    ring: KeyRing

    def _send(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authenticate(self, body: bytes) -> bool:
        try:
            verify(
                self.ring,
                self.command,
                self.path,
                dict(self.headers.items()),
                body,
                self.settings.max_skew_seconds,
            )
        except AuthError as e:
            self._send(HTTPStatus.UNAUTHORIZED, json.dumps({"error": str(e)}).encode())
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(HTTPStatus.OK, json.dumps({"ok": True, "version": __version__}).encode())
            return
        if not self.path.startswith("/v1/jobs/"):
            self._send(HTTPStatus.NOT_FOUND, json.dumps({"error": "no such route"}).encode())
            return
        if not self._authenticate(b""):
            return
        try:
            self._send(*forward(self.settings, "GET", self.path, None))
        except UpstreamError as e:
            self._send(HTTPStatus.BAD_GATEWAY, json.dumps({"error": str(e)}).encode())

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path != "/v1/jobs":
            self._send(HTTPStatus.NOT_FOUND, json.dumps({"error": "no such route"}).encode())
            return
        if not self._authenticate(body):
            return
        try:
            self._send(*forward(self.settings, "POST", self.path, body))
        except UpstreamError as e:
            self._send(HTTPStatus.BAD_GATEWAY, json.dumps({"error": str(e)}).encode())


def build_ring(settings: Settings) -> KeyRing:
    if not settings.signing_key:
        raise SystemExit("GATEWAY_SIGNING_KEY is required")
    key = {settings.key_id: settings.signing_key.encode()}
    return KeyRing(active_id=settings.key_id, keys=key, grace_seconds=KEY_GRACE_SECONDS)


def serve(settings: Settings, ring: KeyRing) -> ThreadingHTTPServer:
    bound = type("BoundHandler", (Handler,), {"settings": settings, "ring": ring})
    return ThreadingHTTPServer((settings.host, settings.port), bound)


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level)
    httpd = serve(settings, build_ring(settings))
    log.info("listening on %s:%d -> %s", settings.host, settings.port, settings.upstream_url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()
