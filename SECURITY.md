# Security Policy

## Supported versions

Only the latest minor release receives fixes.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
("Report a vulnerability" under the Security tab) for this repository. You will get an
acknowledgement within 7 days and a fix or mitigation plan within 30 days for confirmed
issues.

## Scope notes

memware indexes conversation transcripts, which routinely contain secrets pasted into
chats. The database is local and unencrypted by default. Treat `~/.memware/` like you
treat the transcripts themselves, and use the ingest filters (or a pre-ingest scrubber)
if you sync from shared machines.
