#!/usr/bin/env bash
# Pre-publish gate: refuse to ship anything that looks like a personal path,
# host, secret, or real transcript. Extend PATTERNS for your own environment;
# keep the private list out of the repo (see LEAK_CHECK_EXTRA).
set -euo pipefail
cd "$(dirname "$0")/.."
PATTERNS='(/Users/[a-z]+|/home/[a-z]+|C:\\Users|[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|ghp_[A-Za-z0-9]{20,}|BEGIN (RSA|OPENSSH) PRIVATE KEY|Dropbox|launchd|Tailscale|\.local\b|@[a-z0-9.-]+\.(com|io|me|dev)\b)'
EXTRA="${LEAK_CHECK_EXTRA:-}"
[ -n "$EXTRA" ] && PATTERNS="$PATTERNS|($EXTRA)"
if git ls-files -z | xargs -0 grep -nEI "$PATTERNS" -- 2>/dev/null | grep -vE '^(LICENSE|CODE_OF_CONDUCT\.md|scripts/leak-check\.sh):' ; then
  echo "leak-check: FAILED — remove the lines above before publishing" >&2
  exit 1
fi
echo "leak-check: clean"
