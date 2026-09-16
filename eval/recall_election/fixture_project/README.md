# gateway

Edge gateway that fronts the internal job runner. Clients sign each request;
the gateway verifies the signature, forwards the call to the runner and relays
the response. Stdlib only, one process, no framework.

## Quickstart

```sh
cp .env.example .env            # then set GATEWAY_SIGNING_KEY
pip install -e ".[dev]"
gateway                         # listens on GATEWAY_PORT (default 8443)
```

## Routes

- `GET /healthz` - liveness, returns `{"ok": true, "version": ...}`. Unauthenticated on
  purpose: liveness probes carry no signing key, and the route reveals nothing beyond
  liveness and the package version.
- `POST /v1/jobs` - signed; body is forwarded to the runner. Bodies larger than
  `gateway.config.MAX_BODY_BYTES` are refused with 413.
- `GET /v1/jobs/{id}` - signed; forwarded to the runner, status relayed

Upstream calls are attempted up to `gateway.config.RETRY_LIMIT` times, retrying on 5xx and
connection errors with a linear backoff.

## Request signing

Clients send `X-Gateway-Key-Id`, `X-Gateway-Timestamp` and
`X-Gateway-Signature`; `src/gateway/auth.py` documents the canonical string.
Timestamps more than `GATEWAY_MAX_SKEW_SECONDS` from the gateway clock are
rejected. To rotate the signing key without a deploy, call
`gateway.auth.rotate_keys` on the live `KeyRing`; the previous key keeps
verifying for `gateway.config.KEY_GRACE_SECONDS` so in-flight clients are not
cut off.

## Configuration and tests

Every setting is an environment variable; `.env.example` lists them with their
defaults and `gateway.config.load_settings` is the only place they are read.
Run `pytest` for the suite.
