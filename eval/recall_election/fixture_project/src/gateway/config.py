"""Runtime settings for the gateway, loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

RETRY_LIMIT = 5
RETRY_BACKOFF_SECONDS = 0.25
KEY_GRACE_SECONDS = 900
MAX_BODY_BYTES = 262_144


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8443
    upstream_url: str = "http://127.0.0.1:9000"
    upstream_timeout: float = 4.0
    signing_key: str = ""
    key_id: str = "k1"
    max_skew_seconds: int = 300
    log_level: str = "INFO"


def load_settings() -> Settings:
    """Build a Settings from GATEWAY_* environment variables."""
    return Settings(
        host=os.environ.get("GATEWAY_HOST", "0.0.0.0"),
        port=int(os.environ.get("GATEWAY_PORT", "8443")),
        upstream_url=os.environ.get("GATEWAY_UPSTREAM_URL", "http://127.0.0.1:9000"),
        upstream_timeout=float(os.environ.get("GATEWAY_UPSTREAM_TIMEOUT", "4.0")),
        signing_key=os.environ.get("GATEWAY_SIGNING_KEY", ""),
        key_id=os.environ.get("GATEWAY_KEY_ID", "k1"),
        max_skew_seconds=int(os.environ.get("GATEWAY_MAX_SKEW_SECONDS", "300")),
        log_level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO"),
    )
